/**
 * cv_etats.js — Machine à états du bouton « Générer CV ».
 *
 * Fichier SÉPARÉ de la page, et c'est délibéré : le CP0 a acté « un seul
 * fichier autonome, front vanilla, aucune chaîne de build ». Les deux
 * premiers tiennent — pas de build, pas de node_modules, pas d'import de
 * module ES. Le troisième cède sur un point précis : une machine à états
 * enfermée dans une chaîne Python n'est pas testable, et le CP4 exige
 * qu'elle le soit. Le fichier est donc chargé par `<script src>` en
 * production et par `require()` sous `node --test`, sans transformation
 * ni dans un cas ni dans l'autre.
 *
 * Ce module ne touche NI au DOM NI au réseau. Il décide seulement, à
 * partir d'un état, ce que le bouton doit afficher. C'est ce qui le rend
 * testable sans navigateur.
 */
"use strict";

/**
 * Les six états. Gelé : une faute de frappe sur un nom d'état doit
 * échouer bruyamment plutôt que créer une septième branche silencieuse.
 *
 * `actif` = le polling doit continuer tant qu'une offre est dans cet
 * état. Seuls `pending` et `running` bougent tout seuls ; les quatre
 * autres n'évoluent que sur action de l'utilisateur.
 */
const ETATS = Object.freeze({
  idle: Object.freeze({ actif: false }),
  pending: Object.freeze({ actif: true }),
  running: Object.freeze({ actif: true }),
  done: Object.freeze({ actif: false }),
  failed: Object.freeze({ actif: false }),
  text_missing: Object.freeze({ actif: false }),
});

/** Messages lisibles. UN SEUL endroit : la page n'affiche jamais un code brut. */
const MESSAGES_ERREUR = Object.freeze({
  OFFER_NOT_FOUND:
    "Cette offre n'existe plus en base. Relance une recherche.",
  TEXT_MISSING:
    "Le texte de l'annonce n'a pas été récupéré : il n'y a rien à envoyer au modèle.",
  MASTER_INVALID:
    "Ton master.yaml est introuvable ou invalide. Vérifie CV_MASTER_PATH dans config.py.",
  OLLAMA_UNAVAILABLE:
    "Ollama ne répond pas. Lance « ollama serve » puis réessaie.",
  EXTRACTION_FAILED:
    "Le modèle a renvoyé une réponse inexploitable. Réessayer suffit souvent.",
  MATCHING_EMPTY:
    "Aucune expérience de ton master ne correspond à cette offre : le CV serait vide.",
  TYPST_FAILED:
    "La compilation du PDF a échoué. Vérifie que Typst est installé.",
  JOB_NOT_FOUND:
    "Cette génération n'existe plus.",
  PDF_UNAVAILABLE:
    "Le PDF n'est pas disponible. Relance une génération.",
  INTERNAL_ERROR:
    "Erreur interne. Le détail est dans les logs du serveur.",
});

const MESSAGE_INCONNU = "Échec de la génération (code inattendu : %CODE%).";

/**
 * Code d'erreur -> phrase lisible.
 *
 * Un code inconnu NE LÈVE PAS, contrairement à `vueBouton`. La différence
 * est voulue : un état inconnu est un bug de notre code, qu'on veut voir
 * tout de suite ; un code inconnu peut venir d'un serveur plus récent que
 * la page, et faire planter l'affichage de toute la liste pour ça serait
 * disproportionné. On dégrade en montrant le code.
 */
function messageErreur(code) {
  if (!code) return "Échec de la génération.";
  return MESSAGES_ERREUR[code] || MESSAGE_INCONNU.replace("%CODE%", code);
}

/** Vrai si cet état évolue tout seul et justifie donc de continuer à sonder. */
function estActif(etat) {
  const decrit = ETATS[etat];
  if (!decrit) throw new Error(`état inconnu : ${etat}`);
  return decrit.actif;
}

/**
 * Traduit une entrée de `/api/cv/states` en nom d'état.
 * Une offre absente de la réponse est `idle` — c'est le contrat de la route.
 */
function etatDepuis(entree) {
  if (!entree) return "idle";
  const brut = entree.status;
  if (brut === "text_missing") return "text_missing";
  if (brut === "pending" || brut === "running") return brut;
  if (brut === "done") return entree.pdf_url ? "done" : "failed";
  if (brut === "failed") return "failed";
  throw new Error(`statut de job inconnu : ${brut}`);
}

/**
 * Ce que le bouton doit afficher. Rend un objet, jamais du HTML : le
 * rendu est l'affaire de la page, la décision est l'affaire d'ici.
 *
 * La branche `default` LÈVE. C'est l'exhaustivité : ajouter un état sans
 * décider de son bouton doit casser au premier rendu, pas afficher un
 * bouton vide que personne ne remarque.
 */
function vueBouton(etat, infos) {
  const info = infos || {};
  switch (etat) {
    case "idle":
      return {
        libelle: "Générer CV", action: "generer", actif: true,
        classe: "cv-generer",
        titre: "Lance la génération d'un CV adapté à cette offre.",
      };
    case "pending":
      return {
        libelle: info.position ? `En file (n°${info.position})` : "En file",
        action: null, actif: false, classe: "cv-attente",
        titre: "En attente : le worker traite un job à la fois.",
      };
    case "running":
      return {
        libelle: "Génération…", action: null, actif: false,
        classe: "cv-encours", occupe: true,
        titre: "Extraction, matching puis rendu du PDF. Compte plusieurs minutes.",
      };
    case "done":
      return {
        libelle: "Télécharger le CV", action: "telecharger", actif: true,
        classe: "cv-pret", secondaire: { libelle: "Régénérer", action: "generer" },
        titre: "Le PDF est prêt.",
      };
    case "failed":
      return {
        libelle: "Réessayer", action: "generer", actif: true,
        classe: "cv-echec", message: messageErreur(info.error_code),
        titre: messageErreur(info.error_code),
      };
    case "text_missing":
      return {
        libelle: "Texte indisponible", action: null, actif: false,
        classe: "cv-sans-texte", message: messageErreur("TEXT_MISSING"),
        titre: "Lance « python backfill_textes.py --apply » pour récupérer le texte.",
      };
    default:
      throw new Error(`état de génération non géré : ${etat}`);
  }
}

/**
 * Faut-il continuer à sonder ? Vrai dès qu'un SEUL état visible bouge
 * tout seul. C'est ce qui arrête le polling sans avoir à le surveiller
 * ailleurs : la liste des états visibles est la seule source.
 */
function doitSonder(etatsVisibles) {
  return etatsVisibles.some(estActif);
}

const API = {
  ETATS, MESSAGES_ERREUR, messageErreur, estActif,
  etatDepuis, vueBouton, doitSonder,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = API;           // node --test
} else {
  globalThis.CvEtats = API;       // navigateur
}
