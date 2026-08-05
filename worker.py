"""worker.py — Le consommateur UNIQUE de la file de génération de CV.

Seul module de stage_finder à connaître ``cv_forge``, et il n'en touche
qu'une fonction : ``generate_cv``. Aucun autre symbole du paquet n'est
importé nulle part ailleurs.

## Un seul worker

Ollama local ne supporte pas deux générations concurrentes sans
s'écrouler. L'unicité est garantie à DEUX niveaux, volontairement
distincts du jeton (``ollama_pool.JETON``), qui lui ne sérialise que les
appels individuels :

1. ``demarrer()`` refuse de lancer un second thread si un est vivant ;
2. le claim en base est atomique de toute façon (``jobs.reclamer``), donc
   même deux workers ne traiteraient jamais le même job.

Le piège classique est le **reloader de Flask**, qui lance DEUX processus
et donc deux workers dans deux interpréteurs — que ni le verrou ni le
thread ne voient. D'où ``worker_autorise()``, à interroger avant de
démarrer.

## Déchargement

Le modèle reste chaud entre jobs consécutifs : c'est tout l'intérêt d'une
file. Il n'est déchargé qu'à la TRANSITION file non-vide -> file vide,
une seule fois, pas à chaque job terminé.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback as traceback_module
from pathlib import Path

import config
import embeddings_env
import jobs
import ollama_pool
import storage

logger = logging.getLogger(__name__)

# Pause entre deux sondages de la file quand elle est vide. Assez court
# pour qu'un clic parte vite, assez long pour ne pas marteler SQLite.
INTERVALLE_SONDAGE_S = 1.0


def worker_autorise(*, reloader_actif: bool = False, env: dict | None = None) -> bool:
    """Vrai si CE processus est celui qui doit porter le worker.

    Le reloader de Werkzeug exécute le script dans DEUX interpréteurs : un
    superviseur qui surveille les fichiers, et un enfant qui sert. Deux
    workers y tourneraient, invisibles l'un à l'autre — ni le verrou de
    thread ni le drapeau d'instance ne traversent un processus — donc deux
    générations Ollama simultanées. C'est exactement la panne que
    l'unicité existe pour empêcher.

    Seul l'enfant porte ``WERKZEUG_RUN_MAIN=true``. Chez le superviseur la
    variable est ABSENTE — comme lorsqu'il n'y a pas de reloader du tout.
    Les deux situations sont donc indiscernables depuis l'environnement
    seul, et c'est pourquoi ``reloader_actif`` est un paramètre EXPLICITE
    plutôt qu'une déduction : l'appelant sait ce qu'il a passé à
    ``app.run()``, le worker ne peut que le deviner.
    """
    env = os.environ if env is None else env
    if not reloader_actif:
        return True
    return env.get("WERKZEUG_RUN_MAIN") == "true"


class Worker:
    """Consomme la file, un job à la fois, jusqu'à ``arreter()``."""

    def __init__(
        self,
        *,
        db_path: str | None = None,
        master_path: Path | None = None,
        out_root: Path | None = None,
        forge_config=None,
        generer=None,
        decharger=None,
        intervalle: float = INTERVALLE_SONDAGE_S,
    ) -> None:
        self.db_path = db_path or config.CHEMIN_BASE
        # `Path()` sur la valeur RETENUE, jamais sur la seule branche
        # injectée : la version précédente convertissait l'argument de test
        # et laissait passer la constante de `config.py` telle quelle. Les
        # tests, qui injectent toujours, ne pouvaient pas voir le défaut —
        # la chaîne était verte avec une configuration inutilisable.
        # `Path(Path(...))` est l'identité, donc les deux branches sont sûres.
        self.master_path = Path(master_path or config.CV_MASTER_PATH)
        self.out_root = Path(out_root or config.CV_OUT_ROOT)
        self._forge_config = forge_config
        self.intervalle = intervalle

        # Indirections injectables : la suite de tests ne doit jamais
        # appeler le vrai Ollama ni le vrai Typst.
        self._generer = generer
        self._decharger = decharger or ollama_pool.decharger

        self._thread: threading.Thread | None = None
        self._arret = threading.Event()
        # Vrai dès qu'un job a été traité et tant que la file n'a pas été
        # trouvée vide. C'est CE drapeau qui rend le déchargement unique :
        # sans lui, chaque tour à vide re-déchargerait un modèle déjà parti.
        self._file_active = False
        self.decharges = 0            # compteur, lu par les tests

    # -- configuration paresseuse -------------------------------------
    def config_forge(self):
        """``ForgeConfig`` construite à la demande, jamais à l'import.

        Importer ``cv_forge`` au chargement du module ferait payer le
        paquet à tout usage de stage_finder, y compris au CLI de collecte
        qui n'a rien à voir avec les CV.
        """
        if self._forge_config is None:
            from cv_forge import ForgeConfig

            self._forge_config = ForgeConfig()
        return self._forge_config

    def _appel_generate_cv(self, offre: dict, texte: str):
        # AVANT l'import : `cv_forge.match` charge l'encodeur au premier
        # texte inconnu, et `sentence_transformers` interroge alors Hugging
        # Face. Derrière une interception TLS, cet appel échoue en
        # CERTIFICATE_VERIFY_FAILED — ce qui est arrivé au premier vrai run.
        # `ranker.py` réglait déjà le problème pour le classement ; ce
        # chemin-ci ne passait par aucun des deux points d'activation.
        embeddings_env.preparer()

        from cv_forge import OfferInput, generate_cv

        entree = OfferInput(
            offer_id=offre["cle"],
            title=offre["title"] or "",
            company=offre["company"] or "",
            raw_text=texte,
            url=offre["url"],
        )
        sortie = self.out_root / offre["cle"][:16]
        # Le jeton entoure l'APPEL, pas le job : c'est le seul moment où
        # une inférence est réellement en vol.
        with ollama_pool.JETON:
            return generate_cv(
                entree,
                master_path=self.master_path,
                out_dir=sortie,
                config=self.config_forge(),
            )

    # -- cycle de vie --------------------------------------------------
    def demarrer(self) -> bool:
        """Lance le thread. Rend False si un worker tourne déjà."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Worker déjà en cours : second démarrage ignoré.")
            return False
        self._arret.clear()
        self._thread = threading.Thread(
            target=self._boucle, name="cv-worker", daemon=True
        )
        self._thread.start()
        return True

    def arreter(self, timeout: float = 5.0) -> None:
        self._arret.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def actif(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- boucle ---------------------------------------------------------
    def _boucle(self) -> None:
        conn = storage.ouvrir(self.db_path)
        jobs.ensure_schema(conn)
        repris, abandonnes = jobs.reprendre_orphelins(conn)
        if repris or abandonnes:
            logger.info("Reprise : %d job(s) remis en file, %d abandonné(s).",
                        repris, abandonnes)
        try:
            while not self._arret.is_set():
                if not self._traiter_un(conn):
                    self._au_repos()
                    self._arret.wait(self.intervalle)
        finally:
            conn.close()

    def _au_repos(self) -> None:
        """File vide. Décharge le modèle SI on vient de finir de travailler.

        La condition est une TRANSITION, pas un état : sans le drapeau, un
        worker au repos redemanderait le déchargement à chaque seconde.
        """
        if not self._file_active:
            return
        self._file_active = False
        modele = self.config_forge().resolved()["model"]
        try:
            if self._decharger(modele):
                self.decharges += 1
        except Exception:  # noqa: BLE001 - un déchargement ne casse rien
            logger.debug("Déchargement de %s en échec.", modele, exc_info=True)

    def _traiter_un(self, conn) -> bool:
        """Traite un job. Rend False si la file est vide."""
        job = jobs.reclamer(conn)
        if job is None:
            return False
        self._file_active = True

        offre = conn.execute(
            "SELECT cle, title, company, url FROM offres WHERE cle = ?",
            (job["offer_id"],),
        ).fetchone()
        if offre is None:
            jobs.echouer(conn, job["id"],
                         error=f"offre {job['offer_id']} introuvable en base",
                         error_code="OFFER_NOT_FOUND")
            return True

        texte = jobs.lire_texte(conn, job["offer_id"])
        if not texte:
            jobs.echouer(
                conn, job["id"],
                error="le texte de l'offre n'est pas en base : rien à envoyer "
                      "au modèle. Utilisez « Récupérer » sur cette offre.",
                error_code="TEXT_MISSING",
            )
            return True

        try:
            resultat = (self._generer or self._appel_generate_cv)(offre, texte)
        except Exception as exc:  # noqa: BLE001 - le worker ne meurt jamais
            # `generate_cv` promet de ne pas lever ; si ça arrive quand
            # même, c'est CE thread qui tomberait et la file entière avec
            # lui. La pile est persistée : personne ne lit stderr d'un
            # thread de fond.
            jobs.echouer(conn, job["id"], error=f"{type(exc).__name__}: {exc}",
                         error_code="INTERNAL_ERROR",
                         trace=traceback_module.format_exc())
            logger.exception("Job %s : exception inattendue.", job["id"])
            return True

        if resultat.status == "done":
            jobs.terminer(conn, job["id"],
                          str(resultat.pdf_path) if resultat.pdf_path else None)
        else:
            code = resultat.error_code
            jobs.echouer(
                conn, job["id"], error=resultat.error or "échec sans message",
                error_code=getattr(code, "value", code),
                # `generate_cv` attrape les exceptions imprévues et ne les
                # relève pas : sans ce champ, la pile resterait dans SON
                # logger et la ligne de job n'aurait que le message. C'est
                # exactement ce qui est arrivé au premier vrai run.
                trace=getattr(resultat, "traceback", None),
            )
        return True
