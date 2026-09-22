"""
ranker.py — Cœur du projet : classement des offres par similarité sémantique
avec un (ou plusieurs) profil(s) de référence défini(s) dans config.py.

Fonctionnement :
1) Chargement d'un modèle sentence-transformers MULTILINGUE, 100 % en local
   (aucune clé API, aucune donnée qui sort de la machine).
   -> indispensable car les offres sont en français.
2) Encodage de chaque profil de référence (une ou plusieurs ancres) et de chaque
   offre (titre + description). On surveille et on borne la TRONCATURE.
3) Similarité cosinus offre ↔ profils, agrégée (max ou moyenne pondérée).
4) Composition du score final selon config.MODE_COMPOSITION_SCORE :
   additif / additif normalisé / multiplicatif / lexicographique.
5) Tri décroissant.

Le modèle est téléchargé une seule fois puis mis en cache localement par
HuggingFace ; les exécutions suivantes sont hors-ligne.
"""

from __future__ import annotations

import logging
import re

import numpy as np

import config
import embeddings_env
import extract
from normalize import Offre

# Le magasin de certificats de l'OS, pour le cas où le modèle doit encore
# être téléchargé (même problématique TLS que les sources : proxy ou
# antivirus qui intercepte). `embeddings_env.activer_truststore` fait
# exactement cela ; on l'appelle ici plutôt que de recopier le bloc, et à
# l'import plutôt qu'au chargement, pour que l'effet précède tout usage
# du réseau par ce module.
embeddings_env.activer_truststore()

logger = logging.getLogger(__name__)

# Le modèle sentence-transformers est chargé paresseusement (au premier appel)
# et mémorisé, avec son nom : demander un AUTRE modèle (benchmark, dédup sur
# MiniLM pendant que le classement tourne sur un autre) le recharge.
_modele = None
_nom_modele: str | None = None

# Préfixe d'un modèle servi par Ollama plutôt que par sentence-transformers.
PREFIXE_OLLAMA = "ollama:"


def est_ollama(nom: str) -> bool:
    return nom.startswith(PREFIXE_OLLAMA)


class EncodeurOllama:
    """Encodeur au même contrat que ``SentenceTransformer.encode``, via Ollama.

    Les vecteurs passent par ``cache_embeddings`` : calculés une fois par
    (modèle, texte), relus d'un run à l'autre. Pas de tokenizer local, donc
    pas de diagnostic de troncature (``max_seq_length = None``) : bge-m3 lit
    8192 tokens, et Ollama tronque au-delà (``truncate``).
    """

    max_seq_length = None

    def __init__(self, nom: str) -> None:
        self.nom = nom[len(PREFIXE_OLLAMA):] if est_ollama(nom) else nom

    def encode(self, textes, normalize_embeddings: bool = False, **_ignores) -> np.ndarray:
        import cache_embeddings

        vecteurs = cache_embeddings.encoder(list(textes), self.nom)
        if normalize_embeddings and len(vecteurs):
            normes = np.linalg.norm(vecteurs, axis=1, keepdims=True)
            vecteurs = vecteurs / np.where(normes == 0, 1.0, normes)
        return vecteurs


def _charger_modele(nom: str | None = None):
    """Charge (une fois) le modèle d'embeddings et borne la troncature.

    ``nom`` : défaut ``config.MODELE_EMBEDDING``. Un nom « ollama:… » rend un
    ``EncodeurOllama`` — rien à charger ici, le modèle vit dans Ollama.

    ## Hors ligne : ici, et pas au démarrage de chaque exécutable

    `HF_HUB_OFFLINE` doit être posé AVANT que `huggingface_hub` ne soit
    importé — la lib recopie la variable dans une constante de module à
    l'import et ne la relit jamais. Le poser dans `main.py` et dans
    `app.py` marcherait, et laisserait le trou ouvert pour la troisième
    porte d'entrée qu'on oubliera : `evaluation.py`, `benchmark.py`, ou
    un script à venir.

    Ce point-ci est le VRAI goulot : c'est la seule ligne du projet qui
    importe `sentence_transformers` pour le classement, et elle est
    exécutée avant lui. Toute porte d'entrée passe par elle, y compris
    celles qui n'existent pas encore.

    Le projet revendique « 100 % local » ; sans ce réglage, chaque
    chargement va demander à Hugging Face si le modèle a changé — d'où le
    message réclamant un `HF_TOKEN`. Sur une machine sans réseau, le
    chargement ÉCHOUE alors que le modèle est en cache (constaté).
    """
    global _modele, _nom_modele
    nom = nom or config.MODELE_EMBEDDING
    if est_ollama(nom):
        return EncodeurOllama(nom)
    if _modele is None or _nom_modele != nom:
        # AVANT l'import : `preparer` ne peut plus rien pour une lib déjà
        # chargée (elle corrige la constante quand elle peut, mais mieux
        # vaut ne pas en dépendre).
        etat = embeddings_env.preparer(nom)
        if not etat["hors_ligne"]:
            logger.info(
                "Modèle absent du cache Hugging Face : téléchargement autorisé "
                "pour cette fois (une seule).")

        # Import tardif : évite de payer le coût de torch tant qu'on ne classe pas.
        from sentence_transformers import SentenceTransformer

        logger.info("Chargement du modèle « %s »…", nom)
        _modele = SentenceTransformer(nom)
        _nom_modele = nom
        if config.MAX_SEQ_LENGTH is not None:
            # On fixe explicitement la fenêtre : sans ça, la troncature se fait en
            # silence à la valeur par défaut du modèle (souvent 128 tokens), et
            # les descriptions longues sont coupées sans qu'on le sache.
            _modele.max_seq_length = config.MAX_SEQ_LENGTH
        logger.info("Modèle chargé (max_seq_length = %s).", _modele.max_seq_length)
    return _modele


def modeles_ollama_requis() -> list[str]:
    """Modèles d'embeddings que le classement demandera à Ollama (check de démarrage)."""
    noms = {config.MODELE_EMBEDDING, config.MODELE_EMBEDDING_DEDUP}
    return sorted(EncodeurOllama(n).nom for n in noms if est_ollama(n))


def liberer_modele(nom: str | None = None) -> None:
    """Décharge d'Ollama le modèle d'embeddings, s'il y est servi.

    Appelé entre le classement et la vérification LLM : sur 6 Go de VRAM,
    bge-m3 et le modèle de génération tiennent ensemble de justesse. On
    libère donc la place AVANT de charger le second. Sans effet pour un
    modèle sentence-transformers (CPU, dans ce processus).
    """
    nom = nom or config.MODELE_EMBEDDING
    if est_ollama(nom):
        import ollama_pool

        ollama_pool.decharger(EncodeurOllama(nom).nom)


def _texte_a_encoder(offre: Offre) -> str:
    """Texte représentatif d'une offre pour l'embedding (titre + description)."""
    base = f"{offre.title}. {offre.description}".strip()
    return f"{config.PREFIXE_DOCUMENT}{base}"


def _surveiller_troncature(modele, textes: list[str]) -> None:
    """Loggue combien d'offres dépassent la fenêtre du modèle (donc tronquées).

    On ne bloque rien : on rend juste visible un phénomène habituellement
    silencieux, pour savoir si le classement se joue sur des textes coupés.
    """
    try:
        limite = int(modele.max_seq_length)
        tokenizer = modele.tokenizer
    except Exception:  # noqa: BLE001 - tokenizer indisponible : on renonce au diagnostic
        return
    tronquees = 0
    for texte in textes:
        # Tokenisation sans troncature pour compter la longueur réelle.
        n = len(tokenizer.encode(texte, add_special_tokens=True, truncation=False))
        if n > limite:
            tronquees += 1
    if tronquees:
        logger.info(
            "Troncature : %d/%d offre(s) dépassent %d tokens et sont coupées.",
            tronquees, len(textes), limite,
        )


# ---------------------------------------------------------------------------
# Boost par mots-clés (re-ranking par-dessus la similarité)
# ---------------------------------------------------------------------------
def _motif(mots: list[str]) -> re.Pattern:
    """Compile un motif « mot entier » insensible à la casse."""
    return re.compile(r"\b(?:" + "|".join(re.escape(m) for m in mots) + r")\b", re.I)


_MOTIF_IA = _motif(config.MOTS_CLES_IA)
_MOTIF_CYBER = _motif(config.MOTS_CLES_CYBER)


def _presence(motif: re.Pattern, titre: str, desc: str) -> tuple[bool, bool]:
    """Retourne (présent_dans_titre, présent_dans_description)."""
    return bool(motif.search(titre)), bool(motif.search(desc))


def _boost_categorie(dans_titre: bool, dans_desc: bool, poids: float) -> float:
    """Boost d'une catégorie : plein si dans le titre, atténué si seulement en desc."""
    if dans_titre:
        return poids
    if dans_desc:
        return poids * config.BOOST_FACTEUR_DESCRIPTION
    return 0.0


def calculer_boost(offre: Offre) -> tuple[float, list[str]]:
    """
    Calcule le bonus de score d'une offre selon ses mots-clés IA / cyber.

    Un mot-clé dans le titre pèse plein pot ; seulement dans la description, il
    est atténué (anti-boilerplate). Le bonus combo n'est accordé que si IA ET
    cyber sont présents ET qu'au moins l'un des deux figure dans le titre.

    Retourne (boost, tags) où tags sert aux badges d'affichage.
    """
    ia_titre, ia_desc = _presence(_MOTIF_IA, offre.title, offre.description)
    cy_titre, cy_desc = _presence(_MOTIF_CYBER, offre.title, offre.description)
    a_ia = ia_titre or ia_desc
    a_cyber = cy_titre or cy_desc

    boost = 0.0
    tags: list[str] = []
    if a_ia:
        boost += _boost_categorie(ia_titre, ia_desc, config.BOOST_IA)
        tags.append("IA")
    if a_cyber:
        boost += _boost_categorie(cy_titre, cy_desc, config.BOOST_CYBER)
        tags.append("Cyber")

    # Combo : IA ET cyber, avec au moins un signal dans le titre (si exigé).
    signal_titre = ia_titre or cy_titre
    if a_ia and a_cyber and (signal_titre or not config.COMBO_EXIGE_SIGNAL_TITRE):
        boost += config.BOOST_COMBO
        tags.append("★ IA+CYBER")

    return boost, tags


# ---------------------------------------------------------------------------
# Embeddings : profils de référence + offres
# ---------------------------------------------------------------------------
def _profils_reference() -> list[dict]:
    """Liste des profils de référence (fallback sur REQUETE_REFERENCE si vide)."""
    if config.PROFILS_REFERENCE:
        return config.PROFILS_REFERENCE
    return [{"texte": config.REQUETE_REFERENCE, "poids": 1.0}]


def encoder_offres(offres: list[Offre], modele: str | None = None) -> np.ndarray:
    """Encode les offres en embeddings NORMALISÉS (produit scalaire = cosinus).

    Exposé pour être réutilisé par la dédup floue (dedup.py) et l'évaluation
    (eval.py), afin de ne pas recalculer les embeddings plusieurs fois.
    ``modele`` : défaut ``config.MODELE_EMBEDDING`` ; la dédup passe
    ``config.MODELE_EMBEDDING_DEDUP``.
    """
    modele = _charger_modele(modele)
    textes = [_texte_a_encoder(o) for o in offres]
    _surveiller_troncature(modele, textes)
    return modele.encode(
        textes, normalize_embeddings=True, batch_size=32, show_progress_bar=False
    )


def similarites_reference(emb_offres: np.ndarray) -> np.ndarray:
    """Similarité de chaque offre aux profils de référence, agrégée.

    - "max"     : la meilleure ancre décide (une offre « IA pure » n'est pas
                  pénalisée de ne pas ressembler au profil cyber).
    - "moyenne" : moyenne pondérée par les poids des profils.
    """
    modele = _charger_modele()
    profils = _profils_reference()
    textes = [f"{config.PREFIXE_REQUETE}{p['texte']}" for p in profils]
    poids = np.array([float(p["poids"]) for p in profils])
    emb_profils = modele.encode(textes, normalize_embeddings=True)

    # Matrice (n_offres, n_profils) des cosinus.
    sims = np.asarray(emb_offres) @ np.asarray(emb_profils).T

    if config.AGGREGATION_PROFILS == "moyenne":
        return (sims * poids).sum(axis=1) / poids.sum()
    # "max" pondéré : on pondère avant de prendre le meilleur profil.
    return (sims * poids).max(axis=1)


# ---------------------------------------------------------------------------
# Composition du score
# ---------------------------------------------------------------------------
def _normaliser(vec: np.ndarray, methode: str) -> np.ndarray:
    """Normalise un vecteur de scores (min-max dans [0,1] ou z-score centré)."""
    v = np.asarray(vec, dtype=float)
    if v.size == 0:
        return v
    if methode == "zscore":
        mu, sd = v.mean(), v.std()
        return (v - mu) / sd if sd > 1e-9 else np.zeros_like(v)
    # min-max par défaut
    lo, hi = v.min(), v.max()
    return (v - lo) / (hi - lo) if hi - lo > 1e-9 else np.zeros_like(v)


def _tier(tags: list[str], boost_mc: float) -> int:
    """Palier lexicographique : 2 = combo, 1 = un boost IA/cyber, 0 = aucun."""
    if "★ IA+CYBER" in tags:
        return 2
    if boost_mc > 0:
        return 1
    return 0


def composer(
    offres: list[Offre],
    sims: np.ndarray,
    boosts_mc: np.ndarray,
    tags_par_offre: list[list[str]],
    boosts_soft: np.ndarray,
) -> list[tuple[Offre, float]]:
    """Combine similarité + boosts en un classement selon le mode configuré."""
    mode = config.MODE_COMPOSITION_SCORE
    boost_total = boosts_mc + boosts_soft

    if mode == "additif":
        scores = np.minimum(1.0, sims + boost_total)
        cle = scores
    elif mode == "additif_normalise":
        base = _normaliser(sims, config.METHODE_NORMALISATION)
        scores = base + boost_total
        cle = scores
    elif mode == "multiplicatif":
        # cosinus × (1 + boost) : le boost module sans jamais dominer.
        scores = sims * (1.0 + boost_total)
        cle = scores
    elif mode == "lexicographique":
        # Score affiché = cosinus ; tri : palier (combo > boost > rien) puis cosinus.
        scores = sims
        tiers = np.array([_tier(t, float(b)) for t, b in zip(tags_par_offre, boosts_mc)])
        # Clé composite encodée en un float : palier prioritaire, cosinus en second.
        cle = tiers * 1000.0 + sims
    else:  # sécurité : ne devrait pas arriver (pydantic valide en amont)
        scores = np.minimum(1.0, sims + boost_total)
        cle = scores

    couples = list(zip(offres, [float(s) for s in scores], [float(c) for c in cle]))
    couples.sort(key=lambda x: x[2], reverse=True)
    return [(o, s) for (o, s, _c) in couples]


def classer(
    offres: list[Offre], embeddings: np.ndarray | None = None
) -> list[tuple[Offre, float]]:
    """
    Classe les offres par pertinence décroissante.

    ``embeddings`` : embeddings normalisés déjà calculés (ex. par la dédup
    floue). S'ils sont fournis, on évite de ré-encoder les offres.

    Retourne une liste de tuples (offre, score), triée du plus au moins pertinent.
    """
    if not offres:
        return []

    emb_offres = encoder_offres(offres) if embeddings is None else np.asarray(embeddings)
    sims = similarites_reference(emb_offres)

    # Boost mots-clés + signaux souples (durée / date de début).
    boosts_mc, boosts_soft, tags_par_offre = [], [], []
    nb_combo = 0
    for offre in offres:
        boost, tags = calculer_boost(offre)
        offre.tags = tags
        if "★ IA+CYBER" in tags:
            nb_combo += 1
        boosts_mc.append(boost)
        boosts_soft.append(extract.boost_signaux(offre))
        tags_par_offre.append(tags)

    classees = composer(
        offres,
        sims,
        np.array(boosts_mc),
        tags_par_offre,
        np.array(boosts_soft),
    )
    logger.info(
        "Ranking (%s) : %d offre(s) classée(s), dont %d combo IA+Cyber.",
        config.MODE_COMPOSITION_SCORE, len(classees), nb_combo,
    )
    return classees


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    # Démonstration minimale du ranking sur des offres factices.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    demo = [
        Offre("Stage IA appliquée à la cybersécurité", "TotalEnergies",
              "Paris", "Détection d'intrusion par machine learning, MLOps sécurisé. "
              "Stage de 6 mois à partir de janvier 2027.",
              "http://x", "demo", "", ""),
        Offre("Stage community management", "MediaCorp",
              "Paris", "Animation des réseaux sociaux et rédaction de contenus.",
              "http://y", "demo", "", ""),
    ]
    extract.annoter_toutes(demo)
    for rang, (o, score) in enumerate(classer(demo), 1):
        print(f"{rang}. [{score:.3f}] {' '.join(o.tags)}  {o.title}")
