"""
report.py — Exports des résultats classés : CSV et rapport HTML.

- CSV : ouvrable dans Excel/LibreOffice (encodage utf-8-sig pour les accents).
- HTML : page autonome, lisible, avec offres triées et liens cliquables.

Les deux prennent en entrée la liste classée : [(Offre, score), ...].
"""

from __future__ import annotations

import csv
import html
import logging
from datetime import datetime

from normalize import Offre

logger = logging.getLogger(__name__)

# Colonnes du CSV (ordre stable). duree_mois / date_debut sont les colonnes
# filtrables issues de l'extraction ; nouvelle indique une offre jamais vue.
_COLONNES = [
    "rang", "score", "match", "nouvelle", "title", "company", "location",
    "source", "posted_at", "duree_mois", "date_debut", "salary",
    # Verdict de la vérification LLM (vides pour les offres non vérifiées).
    "score_llm", "pertinent", "niveau", "est_alternance", "drapeaux_rouges",
    "justification", "url", "description",
]


def _champs_verdict(offre: Offre) -> dict:
    """Colonnes issues du verdict LLM (vides si l'offre n'a pas été vérifiée)."""
    verdict = getattr(offre, "verdict", None)
    if verdict is None:
        return {c: "" for c in (
            "score_llm", "pertinent", "niveau", "est_alternance",
            "drapeaux_rouges", "justification",
        )}
    return {
        "score_llm": round(verdict.score, 4),
        "pertinent": "oui" if verdict.pertinent else "non",
        "niveau": verdict.niveau,
        "est_alternance": "oui" if verdict.est_alternance else "non",
        "drapeaux_rouges": " | ".join(verdict.drapeaux_rouges),
        "justification": verdict.justification,
    }


def exporter_csv(classees: list[tuple[Offre, float]], chemin: str) -> None:
    """Écrit les offres classées dans un fichier CSV."""
    with open(chemin, "w", newline="", encoding="utf-8-sig") as f:
        # extrasaction="ignore" : tout champ hors _COLONNES est simplement ignoré.
        writer = csv.DictWriter(f, fieldnames=_COLONNES, extrasaction="ignore")
        writer.writeheader()
        for rang, (offre, score) in enumerate(classees, 1):
            ligne = offre.as_dict()
            ligne["rang"] = rang
            ligne["score"] = round(score, 4)
            ligne["match"] = " ".join(offre.tags)
            ligne["nouvelle"] = "oui" if getattr(offre, "nouvelle", False) else ""
            ligne.update(_champs_verdict(offre))
            writer.writerow(ligne)
    logger.info("CSV écrit : %s (%d offre(s)).", chemin, len(classees))


def _badges_html(tags: list[str]) -> str:
    """Rend les badges de correspondance mots-clés en HTML."""
    if not tags:
        return ""
    morceaux = []
    for tag in tags:
        classe = "badge-combo" if "IA+CYBER" in tag else "badge"
        morceaux.append(f'<span class="{classe}">{html.escape(tag)}</span>')
    return " ".join(morceaux)


def _verdict_html(offre: Offre) -> str:
    """Cellule COMPACTE du verdict LLM : score + pastilles d'alerte, sans texte.

    La justification n'est PAS mise ici : dans une cellule de tableau étroite,
    un paragraphe est illisible (c'était le défaut de la version précédente, où
    il était rogné à trois lignes par un ``overflow: hidden``). Elle est rendue
    sur toute la largeur par ``_ligne_analyse_html``.
    """
    verdict = getattr(offre, "verdict", None)
    if verdict is None:
        return "&mdash;"
    morceaux = [f'<span class="llm-score">{verdict.score:.2f}</span>']
    if not verdict.pertinent:
        morceaux.append('<span class="llm-ko">non pertinent</span>')
    if verdict.est_alternance:
        morceaux.append('<span class="llm-ko">alternance</span>')
    if verdict.domaine_match is False:
        morceaux.append('<span class="llm-ko">hors domaine</span>')
    if verdict.drapeaux_rouges:
        morceaux.append(
            f'<span class="drapeau">🚩 {len(verdict.drapeaux_rouges)}</span>'
        )
    return " ".join(morceaux)


def _ligne_analyse_html(offre: Offre, colonnes: int) -> str:
    """Ligne pleine largeur qui déroule l'analyse du LLM sous l'offre.

    Une ligne <tr> supplémentaire en ``colspan`` : le paragraphe dispose de
    toute la largeur du tableau, sans troncature ni rognage. Rien n'est rendu
    pour une offre non vérifiée (aucune ligne vide parasite).
    """
    verdict = getattr(offre, "verdict", None)
    if verdict is None or not (verdict.justification or verdict.drapeaux_rouges):
        return ""

    drapeaux = "".join(
        f"<li>{html.escape(d)}</li>" for d in verdict.drapeaux_rouges
    )
    bloc_drapeaux = (
        f'<div class="analyse-drapeaux"><strong>🚩 Points de vigilance</strong>'
        f"<ul>{drapeaux}</ul></div>"
        if drapeaux else ""
    )
    texte = (
        f'<p class="analyse-texte">{html.escape(verdict.justification)}</p>'
        if verdict.justification else ""
    )
    meta = " · ".join(
        filter(None, [
            f"score IA {verdict.score:.2f}",
            f"niveau : {html.escape(verdict.niveau)}" if verdict.niveau else "",
            f"durée annoncée : {verdict.duree_mois} mois" if verdict.duree_mois else "",
            f"début : {html.escape(verdict.date_debut)}" if verdict.date_debut else "",
        ])
    )
    return f"""
      <tr class="analyse">
        <td colspan="{colonnes}">
          <div class="analyse-titre">🤖 Analyse du LLM <span class="analyse-meta">{meta}</span></div>
          {texte}
          {bloc_drapeaux}
        </td>
      </tr>"""


def _couleur_score(score: float) -> str:
    """Vert plus ou moins vif selon le score (pour la barre HTML)."""
    if score >= 0.6:
        return "#1a9850"
    if score >= 0.45:
        return "#66bd63"
    if score >= 0.3:
        return "#fee08b"
    return "#d9d9d9"


# Nombre de colonnes du tableau HTML : sert au colspan des lignes d'analyse.
_NB_COLONNES_HTML = 10


def exporter_html(classees: list[tuple[Offre, float]], chemin: str, requete: str) -> None:
    """Écrit un rapport HTML autonome, trié, avec liens cliquables."""
    date = datetime.now().strftime("%d/%m/%Y à %H:%M")
    lignes = []
    for rang, (offre, score) in enumerate(classees, 1):
        pct = max(0, min(100, round(score * 100)))
        neuf = ('<span class="badge-neuf">🆕 Nouveau</span>'
                if getattr(offre, "nouvelle", False) else "")
        duree = f"{offre.duree_mois} mois" if offre.duree_mois else "&mdash;"
        debut = html.escape(offre.date_debut) if offre.date_debut else "&mdash;"
        lignes.append(
            f"""
      <tr class="offre">
        <td class="rang">{rang}</td>
        <td class="score">
          <div class="barre"><span style="width:{pct}%;background:{_couleur_score(score)}"></span></div>
          <span class="val">{score:.3f}</span>
        </td>
        <td class="titre">
          <a href="{html.escape(offre.url)}" target="_blank" rel="noopener">{html.escape(offre.title)}</a>
          <div class="badges">{neuf}{_badges_html(offre.tags)}</div>
        </td>
        <td>{html.escape(offre.company) or "&mdash;"}</td>
        <td>{html.escape(offre.location) or "&mdash;"}</td>
        <td>{duree}</td>
        <td>{debut}</td>
        <td class="llm">{_verdict_html(offre)}</td>
        <td><span class="src">{html.escape(offre.source)}</span></td>
        <td>{html.escape(offre.salary) or "&mdash;"}</td>
      </tr>"""
        )
        # Analyse du LLM sur toute la largeur, juste sous l'offre concernée.
        lignes.append(_ligne_analyse_html(offre, _NB_COLONNES_HTML))

    page = f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stages classés — job-finder</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
           margin: 0; padding: 2rem; background: #f6f7f9; color: #1a1a1a; }}
    h1 {{ margin: 0 0 .3rem; font-size: 1.5rem; }}
    .meta {{ color: #666; margin-bottom: 1.2rem; font-size: .9rem; }}
    .ref {{ background: #fff; border-left: 4px solid #1a9850; padding: .8rem 1rem;
            border-radius: 6px; margin-bottom: 1.5rem; font-size: .92rem; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff;
             border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
    th, td {{ padding: .6rem .7rem; text-align: left; border-bottom: 1px solid #eee;
              font-size: .9rem; vertical-align: middle; }}
    th {{ background: #1f2937; color: #fff; position: sticky; top: 0; font-weight: 600; }}
    tr:hover td {{ background: #f0f7ff; }}
    .rang {{ font-weight: 700; color: #888; width: 2.5rem; }}
    .score {{ width: 130px; }}
    .barre {{ background: #eee; border-radius: 4px; height: 8px; overflow: hidden; }}
    .barre span {{ display: block; height: 100%; }}
    .val {{ font-size: .78rem; color: #555; }}
    .titre a {{ color: #1d4ed8; text-decoration: none; font-weight: 600; }}
    .titre a:hover {{ text-decoration: underline; }}
    .src {{ background: #eef; color: #334; padding: .1rem .45rem; border-radius: 4px; font-size: .78rem; }}
    .badges {{ margin-top: .35rem; display: flex; gap: .3rem; flex-wrap: wrap; }}
    .badge {{ background: #e0edff; color: #1d4ed8; padding: .1rem .5rem; border-radius: 999px;
              font-size: .72rem; font-weight: 600; }}
    .badge-combo {{ background: linear-gradient(90deg,#7c3aed,#db2777); color: #fff;
                    padding: .1rem .55rem; border-radius: 999px; font-size: .72rem; font-weight: 700; }}
    .badge-neuf {{ background: #16a34a; color: #fff; padding: .1rem .5rem; border-radius: 999px;
                   font-size: .72rem; font-weight: 700; }}
    .llm {{ white-space: nowrap; }}
    .llm-score {{ background: #ede9fe; color: #6d28d9; padding: .1rem .45rem; border-radius: 4px;
                  font-size: .78rem; font-weight: 700; }}
    .llm-ko {{ background: #fee2e2; color: #b91c1c; padding: .1rem .45rem; border-radius: 4px;
               font-size: .72rem; font-weight: 600; margin-left: .25rem; }}
    .drapeau {{ display: inline-block; background: #fff7ed; color: #9a3412; padding: .05rem .4rem;
                border-radius: 4px; font-size: .7rem; margin: .15rem .15rem 0 0; }}
    /* Analyse du LLM : ligne pleine largeur, texte de lecture (pas de rognage). */
    tr.analyse td {{ background: #faf5ff; border-bottom: 2px solid #e9d5ff;
                     padding: .7rem 1rem 1rem 3.2rem; }}
    tr.offre td {{ border-bottom: 1px solid #eee; }}
    .analyse-titre {{ font-size: .78rem; font-weight: 700; color: #6d28d9;
                      text-transform: uppercase; letter-spacing: .03em; margin-bottom: .35rem; }}
    .analyse-meta {{ font-weight: 500; color: #7c6a94; text-transform: none;
                     letter-spacing: 0; margin-left: .5rem; }}
    .analyse-texte {{ margin: 0; font-size: .95rem; line-height: 1.65; color: #2b2b2b;
                      max-width: 78ch; }}
    .analyse-drapeaux {{ margin-top: .6rem; font-size: .85rem; color: #9a3412; }}
    .analyse-drapeaux ul {{ margin: .25rem 0 0; padding-left: 1.2rem; line-height: 1.5; }}
    .filtre {{ margin-bottom: 1rem; }}
    .filtre input {{ padding: .5rem .7rem; border: 1px solid #ccc; border-radius: 6px;
                     width: 100%; max-width: 340px; font-size: .9rem; }}
  </style>
</head>
<body>
  <h1>🎯 Stages classés par pertinence</h1>
  <div class="meta">{len(classees)} offre(s) &middot; généré le {date}</div>
  <div class="ref"><strong>Profil de référence :</strong> {html.escape(requete)}</div>
  <div class="filtre">
    <input type="text" id="q" placeholder="🔎 Filtrer (titre, entreprise, lieu, source…)"
           oninput="filtrer()">
  </div>
  <table id="tbl">
    <thead>
      <tr><th>#</th><th>Score</th><th>Titre</th><th>Entreprise</th><th>Lieu</th>
          <th>Durée</th><th>Début</th><th>Vérif LLM</th><th>Source</th><th>Salaire</th></tr>
    </thead>
    <tbody>{''.join(lignes)}
    </tbody>
  </table>
  <script>
    // Filtre client léger : masque les lignes qui ne contiennent pas la requête.
    // Une offre occupe DEUX lignes (l'offre + son analyse LLM) : on décide sur
    // le texte des deux réunies, et on les affiche/masque ensemble — sinon une
    // analyse resterait seule à l'écran, détachée de son offre.
    function filtrer() {{
      const q = document.getElementById('q').value.toLowerCase();
      const lignes = [...document.querySelectorAll('#tbl tbody tr')];
      for (let i = 0; i < lignes.length; i++) {{
        if (!lignes[i].classList.contains('offre')) continue;
        const analyse = lignes[i + 1] && lignes[i + 1].classList.contains('analyse')
                      ? lignes[i + 1] : null;
        const texte = (lignes[i].textContent + ' ' + (analyse ? analyse.textContent : '')).toLowerCase();
        const visible = texte.includes(q) ? '' : 'none';
        lignes[i].style.display = visible;
        if (analyse) analyse.style.display = visible;
      }}
    }}
  </script>
</body>
</html>"""

    with open(chemin, "w", encoding="utf-8") as f:
        f.write(page)
    logger.info("HTML écrit : %s (%d offre(s)).", chemin, len(classees))
