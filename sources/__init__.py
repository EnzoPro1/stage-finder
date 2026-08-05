"""
Package regroupant les sources d'offres (une source = un module indépendant).

Deux effets de bord utiles sont posés ici, une seule fois, au moment où
n'importe quelle source est importée :

1) Activation du magasin de certificats du système d'exploitation via
   ``truststore``. Sur les machines derrière un antivirus ou un proxy qui
   intercepte le TLS (fréquent sous Windows/entreprise), le certificat
   ré-émis n'est pas dans le bundle de ``certifi`` mais bien dans le magasin
   Windows. ``truststore`` fait pointer Python dessus, sans jamais désactiver
   la vérification TLS (on ne fait donc PAS de ``verify=False``).

2) Un utilitaire de masquage des secrets pour les logs.
"""

from __future__ import annotations

import logging
import re

# --- 1) Certificats système (best effort, ne casse jamais l'import) ---------
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - truststore absent ou indisponible : on continue
    # Sans truststore, requests retombe sur certifi (peut échouer derrière un
    # proxy TLS, mais l'erreur sera gérée proprement par chaque source).
    pass


# --- 2) Masquage des secrets dans les messages de log -----------------------
# On couvre les secrets passés en query string (app_key=…) ET les jetons portés
# par un en-tête Authorization (Bearer …) qui pourraient se retrouver dans un log.
_MOTIF_SECRET = re.compile(
    r"(app_id|app_key|api_key|client_secret|client_id|access_token|token|key)=([^&\s]+)",
    re.IGNORECASE,
)
_MOTIF_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)


def masquer_secrets(texte: str) -> str:
    """Remplace la valeur de tout paramètre / jeton sensible par '***'.

    Évite que les clés API n'apparaissent dans les logs quand une exception
    réseau embarque l'URL complète (query string comprise) ou un en-tête.
    """
    t = _MOTIF_SECRET.sub(r"\1=***", str(texte))
    return _MOTIF_BEARER.sub(r"\1***", t)


class RedactingFilter(logging.Filter):
    """Filtre de log qui redacte les secrets AVANT écriture, au niveau du handler.

    ``masquer_secrets()`` seul est un masquage *a posteriori* : il faut penser à
    l'appeler à chaque log. En posant ce filtre sur le handler racine, TOUT
    message (y compris ceux des libs tierces qui logueraient une URL avec clé)
    est redacté systématiquement, sans dépendre de la vigilance de l'appelant.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - args mal formés : on ne bloque pas le log
            return True
        redige = masquer_secrets(message)
        if redige != message:
            # On remplace le message déjà formaté et on neutralise les args pour
            # que le formateur ne les ré-interpole pas (ce qui ré-exposerait la clé).
            record.msg = redige
            record.args = ()
        return True

