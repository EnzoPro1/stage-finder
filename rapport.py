"""
rapport.py — LE rapport HTML : stages et jobs étudiants, dans une seule page.

Lit les deux bases (`config.CHEMIN_BASE`, `config.CHEMIN_BASE_JOBS`), au
DERNIER run de chacune, et écrit `config.CHEMIN_RAPPORT`. Il ne collecte rien :
il se réécrit à la fin de chaque run (`python main.py`, `python main.py
--jobs-etudiants`), et se relance seul (`python rapport.py`).

## Ce que la page montre

- Un EN-TÊTE par section, tiré du bilan enregistré avec le run : sources
  désactivées et pourquoi, conseils (site bloqué, collecte tronquée, trajets
  estimés), offres par source et par famille, et toute source MUETTE — une
  panne silencieuse doit se voir avant la première ligne du tableau.
- Stages : filtrables par famille. Par défaut sur `familles_titre` (un terme
  de la famille est DANS LE TITRE) ; une case élargit aux familles dont la
  requête a trouvé l'offre sans que le titre le dise.
- Jobs étudiants : une colonne par origine, « brut → ajusté » en minutes,
  « estim. » quand le routage a manqué ; tri par meilleur trajet ou par
  origine ; trajet inconnu en dernier.
- Chaque offre : titre, employeur, lieu, TOUS ses liens (un par source),
  étiquettes, date de première vue, marque « nouveau » (nouvelle à ce run).

Seules les lignes rattachées à au moins un lien sont montrées
(`storage.offres_affichables`) : l'orphelin laissé par un changement de titre
reste en base, jamais dans le rapport.

Utilisation :
    python rapport.py                 # écrit flux.html depuis les deux bases
"""

from __future__ import annotations

import html
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config
import filters
import recherche
import storage

logger = logging.getLogger(__name__)


@dataclass
class Section:
    """Le dernier run d'une base, prêt à rendre."""

    titre: str
    run: dict | None
    offres: list[dict] = field(default_factory=list)


def _date_du_run(run: dict):
    try:
        return datetime.fromisoformat(run["horodatage"]).date()
    except (TypeError, ValueError, KeyError):
        return None


def charger_section(titre: str, chemin_base) -> Section:
    """Lit le dernier run d'une base. Base absente ou vide : section vide, jamais d'erreur.

    La base n'est PAS créée si elle n'existe pas : `storage.ouvrir` le ferait,
    et un rapport ne doit rien écrire ailleurs que dans sa page.
    """
    if chemin_base is None or not os.path.exists(chemin_base):
        return Section(titre, None)
    conn = storage.ouvrir(str(chemin_base))
    try:
        run = storage.dernier_run(conn)
        if run is None:
            return Section(titre, None)
        offres = storage.offres_affichables(conn, run["id"])
    finally:
        conn.close()
    for o in offres:
        o["familles"] = (o.get("familles") or "").split()
        o["familles_titre"] = (o.get("familles_titre") or "").split()
        o["drapeaux"] = (o.get("drapeaux") or "").split()
        try:
            o["trajets"] = json.loads(o["trajets"]) if o.get("trajets") else {}
        except ValueError:
            o["trajets"] = {}
        dates = [d for d in [o.get("premiere_vue")] + [l["premiere_vue"] for l in o["liens"]] if d]
        o["premiere_vue"] = min(dates) if dates else ""
        # Âge de l'ANNONCE (date de publication de la source) au jour du run —
        # pas l'âge dans la base : c'est la fraîcheur de l'offre qui compte.
        o["age_jours"] = filters.age_jours(o.get("posted_at") or "",
                                           _date_du_run(run))
    return Section(titre, run, offres)


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def _e(texte) -> str:
    return html.escape(str(texte if texte is not None else ""))


def _libelles_familles() -> dict[str, str]:
    """Étiquette -> libellé lisible (terme d'origine pour les jobs étudiants)."""
    try:
        r = recherche.charger()
    except Exception:  # noqa: BLE001 - un rapport sans libellés reste un rapport
        return {}
    libelles = {nom: nom for nom in r.familles}
    if r.student_jobs:
        libelles.update({nom: f.termes()[0] for nom, f in r.student_jobs.familles().items()})
    libelles[recherche.ETIQUETTE_SANS_MOT_CLE] = "temps partiel (sans mot-clé)"
    libelles[recherche.ETIQUETTE_NON_RETROUVE] = "terme non retrouvé"
    return libelles


def entete(section: Section, libelles: dict[str, str]) -> str:
    """Bloc d'en-tête d'une section : ce qu'un run a vraiment rapporté."""
    if section.run is None:
        return '<p class="vide">Aucun run enregistré dans cette base.</p>'
    run = section.run
    bilan = run.get("bilan") or {}
    lignes = [
        f'<p class="run">Run du {_e(run["horodatage"].replace("T", " "))} — '
        f'<strong>{len(section.offres)}</strong> offre(s) affichée(s), '
        f'<strong>{sum(o["nouvelle"] for o in section.offres)}</strong> nouvelle(s).</p>'
    ]
    if not bilan:
        lignes.append('<p class="alerte">⚠ Aucun bilan enregistré avec ce run '
                      '(lancé hors de <code>main.py</code>) : sources muettes invisibles.</p>')
    muettes = [s["nom"] for s in bilan.get("sources", []) if s.get("brut", 0) == 0]
    if muettes:
        lignes.append('<p class="alerte muette">⚠ MUETTE (0 offre rendue) : '
                      + ", ".join(f"<strong>{_e(n)}</strong>" for n in muettes) + "</p>")
    for message in (bilan.get("conseils") or {}).values():
        lignes.append(f'<p class="alerte">⚠ {_e(message)}</p>')
    for nom, raison in (bilan.get("ecartees") or {}).items():
        lignes.append(f'<p class="ecartee">⊘ <strong>{_e(nom)}</strong> désactivée : {_e(raison)}</p>')

    par_source = Counter(s for o in section.offres for s in {l["source"] for l in o["liens"]})
    brut = {s["nom"]: s for s in bilan.get("sources", [])}
    noms = sorted(set(par_source) | set(brut), key=lambda n: (-par_source.get(n, 0), n))
    cellules = []
    for nom in noms:
        detail = f' <span class="brut">({brut[nom]["brut"]} brut)</span>' if nom in brut else ""
        classe = ' class="zero"' if par_source.get(nom, 0) == 0 else ""
        cellules.append(f"<span{classe}>{_e(nom)} <strong>{par_source.get(nom, 0)}</strong>{detail}</span>")
    lignes.append('<p class="compte"><span class="cle">Par source</span> ' + " · ".join(cellules) + "</p>")

    par_famille = Counter(f for o in section.offres for f in o["familles"])
    familles_bilan = [f["nom"] for f in bilan.get("familles", [])]
    ordre = familles_bilan + sorted(f for f in par_famille if f not in familles_bilan)
    cellules = []
    for f in ordre:
        classe = ' class="zero"' if par_famille.get(f, 0) == 0 else ""
        cellules.append(f"<span{classe}>{_e(libelles.get(f, f))} "
                        f"<strong>{par_famille.get(f, 0)}</strong></span>")
    if cellules:
        lignes.append('<p class="compte"><span class="cle">Par famille</span> ' + " · ".join(cellules) + "</p>")
    return "\n".join(lignes)


def _liens_html(offre: dict) -> str:
    return " ".join(
        f'<a class="lien" href="{_e(l["url"])}" target="_blank" rel="noopener">{_e(l["source"])}</a>'
        for l in offre["liens"])


def _etiquettes_html(offre: dict, libelles: dict[str, str]) -> str:
    puces = [f'<span class="tag{" titre" if f in offre["familles_titre"] else ""}">'
             f'{_e(libelles.get(f, f))}</span>' for f in offre["familles"]]
    puces += [f'<span class="drapeau">⚑ {_e(d)}</span>' for d in offre["drapeaux"]]
    return f'<div class="tags">{"".join(puces)}</div>' if puces else ""


def _age_html(offre: dict) -> str:
    age = offre.get("age_jours")
    return '<td class="num age">—</td>' if age is None else f'<td class="num age">{max(age, 0)} j</td>'


def _neuf(offre: dict) -> str:
    return '<span class="neuf">nouveau</span> ' if offre["nouvelle"] else ""


def section_stages(section: Section, libelles: dict[str, str]) -> str:
    bilan = (section.run or {}).get("bilan") or {}
    familles = [f["nom"] for f in bilan.get("familles", [])]
    familles +=sorted({f for o in section.offres for f in o["familles"]} - set(familles))
    boutons = "".join(f'<button type="button" class="famille" data-famille="{_e(f)}">'
                      f'{_e(libelles.get(f, f))}</button>' for f in familles)
    lignes = []
    for o in section.offres:
        lignes.append(
            f'<tr data-familles="{_e(" ".join(o["familles"]))}" '
            f'data-familles-titre="{_e(" ".join(o["familles_titre"]))}">'
            f'<td class="titre">{_neuf(o)}{_e(o["title"])}{_etiquettes_html(o, libelles)}</td>'
            f'<td>{_e(o["company"]) or "—"}</td><td>{_e(o["location"]) or "—"}</td>'
            f'<td class="liens">{_liens_html(o)}</td>'
            f'<td class="date">{_e(o["premiere_vue"])}</td>{_age_html(o)}'
            f'<td class="num">{(o.get("dernier_score") or 0):.3f}</td></tr>')
    return f"""
<section id="stages">
  <h2>Stages</h2>
  {entete(section, libelles)}
  <div class="outils" data-cible="table-stages">
    <input type="search" class="filtre-texte" placeholder="Filtrer (titre, employeur, lieu…)">
    <div class="familles"><button type="button" class="famille actif" data-famille="">toutes</button>{boutons}</div>
    <label><input type="checkbox" class="elargir"> inclure les familles absentes du titre</label>
  </div>
  <div class="defile"><table id="table-stages">
    <thead><tr><th>Offre</th><th>Employeur</th><th>Lieu</th><th>Liens</th><th>Vue le</th><th>Âge</th><th>Score</th></tr></thead>
    <tbody>{"".join(lignes)}</tbody>
  </table></div>
</section>"""


def _meilleur(offre: dict) -> float | None:
    valeurs = [t["ajuste_min"] for t in offre["trajets"].values() if t.get("ajuste_min") is not None]
    return min(valeurs) if valeurs else None


def section_jobs(section: Section, libelles: dict[str, str], origines: dict[str, str]) -> str:
    entetes = "".join(f'<th><button type="button" class="tri" data-tri="{_e(nom)}">'
                      f'{_e(libelle)}</button></th>' for nom, libelle in origines.items())
    lignes = []
    for o in sorted(section.offres, key=lambda o: (_meilleur(o) is None, _meilleur(o) or 0)):
        cellules = []
        attributs = []
        for nom in origines:
            t = o["trajets"].get(nom)
            if t is None:
                cellules.append('<td class="trajet inconnu">—</td>')
                continue
            attributs.append(f'data-{_e(nom)}="{t["ajuste_min"]}"')
            estim = ' <span class="estim">estim.</span>' if t.get("estimation") else ""
            cellules.append(f'<td class="trajet">{t["brut_min"]:.0f} → <strong>{t["ajuste_min"]:.0f}</strong> min{estim}</td>')
        meilleur = _meilleur(o)
        attributs.append(f'data-meilleur="{meilleur if meilleur is not None else ""}"')
        age = o.get("age_jours")
        attributs.append(f'data-age="{max(age, 0) if age is not None else ""}"')
        lignes.append(
            f'<tr {" ".join(attributs)}>'
            f'<td class="titre">{_neuf(o)}{_e(o["title"])}{_etiquettes_html(o, libelles)}</td>'
            f'<td>{_e(o["company"]) or "—"}</td><td>{_e(o["location"]) or "—"}</td>'
            f'<td class="liens">{_liens_html(o)}</td>'
            f'<td class="date">{_e(o["premiere_vue"])}</td>{_age_html(o)}'
            f'{"".join(cellules)}</tr>')
    return f"""
<section id="jobs">
  <h2>Jobs étudiants</h2>
  {entete(section, libelles)}
  <p class="legende">Âge : jours depuis la publication de l'annonce, au jour du run. Trajet en voiture : durée sans trafic → durée ajustée (× facteur de trafic). « estim. » : routage indisponible, vol d'oiseau × détour. « — » : commune non reconnue.</p>
  <div class="outils" data-cible="table-jobs">
    <input type="search" class="filtre-texte" placeholder="Filtrer (titre, employeur, lieu…)">
    <span>Trier par : <button type="button" class="tri actif" data-tri="meilleur">meilleur trajet</button></span>
  </div>
  <div class="defile"><table id="table-jobs">
    <thead><tr><th>Offre</th><th>Employeur</th><th>Lieu</th><th>Liens</th><th>Vue le</th><th><button type="button" class="tri" data-tri="age">Âge</button></th>{entetes}</tr></thead>
    <tbody>{"".join(lignes)}</tbody>
  </table></div>
</section>"""


_STYLE = """
:root { --fond:#f7f7f5; --carte:#fff; --texte:#1c1c1a; --doux:#6b6b66; --bord:#e3e2dd;
        --accent:#1f5fbf; --alerte:#9a3412; --alerte-fond:#fff4ec; --neuf:#15803d; --tag:#eef2fb; }
@media (prefers-color-scheme: dark) {
  :root { --fond:#141413; --carte:#1d1d1b; --texte:#ecebe6; --doux:#a3a29c; --bord:#34332f;
          --accent:#7aa7ef; --alerte:#fdba74; --alerte-fond:#2a1d14; --neuf:#4ade80; --tag:#1f2a3d; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px; background:var(--fond); color:var(--texte);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1280px; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 4px; } h2 { font-size:1.2rem; margin:32px 0 8px; }
.meta,.legende,.brut,.date { color:var(--doux); font-size:.85rem; }
.run { margin:4px 0; }
.alerte { background:var(--alerte-fond); color:var(--alerte); padding:6px 10px; border-radius:6px; margin:4px 0; }
.ecartee { color:var(--doux); margin:2px 0; font-size:.9rem; }
.compte { margin:4px 0; } .compte .cle { color:var(--doux); margin-right:6px; }
.compte .zero { color:var(--alerte); }
.outils { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin:12px 0; }
.filtre-texte { padding:6px 10px; border:1px solid var(--bord); border-radius:6px; min-width:220px;
                background:var(--carte); color:var(--texte); }
button { font:inherit; font-size:.85rem; padding:3px 10px; border:1px solid var(--bord);
         border-radius:999px; background:var(--carte); color:var(--texte); cursor:pointer; }
button.actif { background:var(--accent); border-color:var(--accent); color:#fff; }
th button { border-radius:6px; }
.defile { overflow-x:auto; background:var(--carte); border:1px solid var(--bord); border-radius:8px; }
table { border-collapse:collapse; width:100%; }
th, td { padding:8px 10px; border-bottom:1px solid var(--bord); text-align:left; vertical-align:top; font-size:.9rem; }
th { position:sticky; top:0; background:var(--carte); font-weight:600; }
td.titre { min-width:260px; font-weight:500; } td.num, td.trajet { white-space:nowrap; }
td.inconnu { color:var(--doux); }
.lien { display:inline-block; margin:0 4px 4px 0; padding:1px 8px; border-radius:4px; background:var(--tag);
        color:var(--accent); text-decoration:none; font-size:.8rem; white-space:nowrap; }
.tags { margin-top:4px; display:flex; flex-wrap:wrap; gap:4px; }
.tag { font-size:.72rem; padding:0 6px; border-radius:999px; border:1px solid var(--bord); color:var(--doux); }
.tag.titre { border-color:var(--accent); color:var(--accent); }
.drapeau { font-size:.72rem; color:var(--alerte); }
.neuf { font-size:.7rem; font-weight:700; color:#fff; background:var(--neuf); padding:0 6px; border-radius:999px; }
.estim { font-size:.7rem; color:var(--alerte); }
"""

_SCRIPT = """
document.querySelectorAll('.outils').forEach(function (outils) {
  var table = document.getElementById(outils.dataset.cible);
  var corps = table.tBodies[0];
  var texte = outils.querySelector('.filtre-texte');
  var elargir = outils.querySelector('.elargir');
  var famille = '';
  function appliquer() {
    var q = texte.value.toLowerCase();
    Array.prototype.forEach.call(corps.rows, function (ligne) {
      var ok = !q || ligne.textContent.toLowerCase().indexOf(q) >= 0;
      if (ok && famille) {
        var attribut = elargir && elargir.checked ? 'familles' : 'famillesTitre';
        ok = (' ' + (ligne.dataset[attribut] || '') + ' ').indexOf(' ' + famille + ' ') >= 0;
      }
      ligne.style.display = ok ? '' : 'none';
    });
  }
  texte.addEventListener('input', appliquer);
  if (elargir) elargir.addEventListener('change', appliquer);
  outils.querySelectorAll('.famille').forEach(function (b) {
    b.addEventListener('click', function () {
      outils.querySelectorAll('.famille').forEach(function (x) { x.classList.remove('actif'); });
      b.classList.add('actif'); famille = b.dataset.famille; appliquer();
    });
  });
  document.querySelectorAll('[data-tri]').forEach(function (b) {
    if (!table.contains(b) && !outils.contains(b)) return;
    b.addEventListener('click', function () {
      var cle = b.dataset.tri;
      document.querySelectorAll('#' + table.id + ' [data-tri], [data-cible="' + table.id + '"] [data-tri]')
        .forEach(function (x) { x.classList.remove('actif'); });
      b.classList.add('actif');
      var lignes = Array.prototype.slice.call(corps.rows);
      lignes.sort(function (a, c) {
        var va = parseFloat(a.dataset[cle]), vc = parseFloat(c.dataset[cle]);
        if (isNaN(va)) return isNaN(vc) ? 0 : 1;
        if (isNaN(vc)) return -1;
        return va - vc;
      });
      lignes.forEach(function (l) { corps.appendChild(l); });
    });
  });
});
"""


def rendre(stages: Section, jobs: Section, origines: dict[str, str]) -> str:
    """Page HTML complète, autonome."""
    libelles = _libelles_familles()
    genere = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flux d'offres</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
  <h1>Flux d'offres</h1>
  <p class="meta">Généré le {_e(genere)} — dernier run de chaque base.</p>
  {section_stages(stages, libelles)}
  {section_jobs(jobs, libelles, origines)}
</main>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def _origines() -> dict[str, str]:
    try:
        jobs = recherche.charger().student_jobs
    except Exception:  # noqa: BLE001
        return {}
    return {nom: o.libelle for nom, o in jobs.origines.items()} if jobs else {}


def generer(chemin_stages=None, chemin_jobs=None, sortie=None) -> Path:
    """Écrit le rapport unifié. Rend le chemin écrit."""
    stages = charger_section("Stages", chemin_stages if chemin_stages is not None else config.CHEMIN_BASE)
    jobs = charger_section("Jobs étudiants",
                           chemin_jobs if chemin_jobs is not None else config.CHEMIN_BASE_JOBS)
    sortie = Path(sortie if sortie is not None else config.CHEMIN_RAPPORT)
    sortie.write_text(rendre(stages, jobs, _origines()), encoding="utf-8")
    logger.info("Rapport écrit : %s (%d stage(s), %d job(s) étudiant(s)).",
                sortie, len(stages.offres), len(jobs.offres))
    return sortie


if __name__ == "__main__":
    import console  # noqa: F401 - force UTF-8 sur la console Windows

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print(f"✅ {generer()}")
