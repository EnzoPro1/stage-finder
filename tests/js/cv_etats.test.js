/**
 * Machine à états du bouton « Générer CV ».
 *
 * Exécuté par `node --test`, intégré au runtime : aucun node_modules,
 * aucune chaîne de build. `tests/test_front_js.py` le lance depuis
 * pytest pour qu'une seule commande couvre tout le projet.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const {
  ETATS, MESSAGES_ERREUR, messageErreur, estActif,
  etatDepuis, vueBouton, doitSonder,
} = require(path.join(__dirname, "..", "..", "static", "cv_etats.js"));

const TOUS = ["idle", "pending", "running", "done", "failed", "text_missing"];

// =====================================================================
// Exhaustivité
// =====================================================================
test("les six états du CP4, ni plus ni moins", () => {
  assert.deepStrictEqual(Object.keys(ETATS).sort(), [...TOUS].sort());
});

test("la table des états est gelée", () => {
  assert.ok(Object.isFrozen(ETATS));
  assert.throws(() => { "use strict"; ETATS.nouveau = {}; });
});

test("chaque état connu rend une vue de bouton", () => {
  for (const etat of TOUS) {
    const vue = vueBouton(etat, {});
    assert.ok(vue.libelle, `${etat} sans libellé`);
    assert.strictEqual(typeof vue.actif, "boolean", `${etat} sans actif`);
  }
});

test("un état inconnu LÈVE plutôt que d'afficher un bouton vide", () => {
  for (const faux of ["", "pendingg", "DONE", "en_cours", null, undefined, 42]) {
    assert.throws(() => vueBouton(faux, {}), /non géré/,
      `${JSON.stringify(faux)} aurait dû lever`);
  }
});

test("estActif lève aussi sur un état inconnu", () => {
  assert.throws(() => estActif("inconnu"), /état inconnu/);
});

// =====================================================================
// Les transitions, une par une
// =====================================================================
test("idle → bouton Générer CV, cliquable", () => {
  const vue = vueBouton("idle", {});
  assert.strictEqual(vue.libelle, "Générer CV");
  assert.strictEqual(vue.action, "generer");
  assert.strictEqual(vue.actif, true);
});

test("pending → position affichée, bouton désactivé", () => {
  const vue = vueBouton("pending", { position: 3 });
  assert.strictEqual(vue.libelle, "En file (n°3)");
  assert.strictEqual(vue.actif, false);
  assert.strictEqual(vue.action, null);
});

test("pending sans position reste lisible", () => {
  assert.strictEqual(vueBouton("pending", {}).libelle, "En file");
});

test("running → désactivé et marqué occupé", () => {
  const vue = vueBouton("running", {});
  assert.strictEqual(vue.actif, false);
  assert.strictEqual(vue.occupe, true);
});

test("done → téléchargement, plus une action secondaire Régénérer", () => {
  const vue = vueBouton("done", {});
  assert.strictEqual(vue.action, "telecharger");
  assert.strictEqual(vue.actif, true);
  assert.strictEqual(vue.secondaire.action, "generer");
});

test("failed → Réessayer, avec un message lisible et pas un code", () => {
  const vue = vueBouton("failed", { error_code: "OLLAMA_UNAVAILABLE" });
  assert.strictEqual(vue.libelle, "Réessayer");
  assert.strictEqual(vue.actif, true);
  assert.match(vue.message, /ollama serve/);
  assert.ok(!vue.message.includes("OLLAMA_UNAVAILABLE"));
});

test("text_missing → désactivé, avec l'explication", () => {
  const vue = vueBouton("text_missing", {});
  assert.strictEqual(vue.actif, false);
  assert.strictEqual(vue.action, null);
  assert.match(vue.message, /rien à envoyer/);
});

// =====================================================================
// Polling : ne tourne QUE s'il y a de quoi
// =====================================================================
test("seuls pending et running justifient de sonder", () => {
  assert.strictEqual(estActif("pending"), true);
  assert.strictEqual(estActif("running"), true);
  for (const fixe of ["idle", "done", "failed", "text_missing"]) {
    assert.strictEqual(estActif(fixe), false, `${fixe} ne doit pas sonder`);
  }
});

test("aucun job actif → aucun polling", () => {
  assert.strictEqual(doitSonder([]), false);
  assert.strictEqual(doitSonder(["idle", "done", "failed", "text_missing"]), false);
});

test("un seul job actif suffit à sonder", () => {
  assert.strictEqual(doitSonder(["idle", "done", "pending"]), true);
  assert.strictEqual(doitSonder(["running"]), true);
});

test("le polling s'arrête dès que le dernier job devient terminal", () => {
  let etats = ["done", "running", "idle"];
  assert.strictEqual(doitSonder(etats), true);
  etats = ["done", "done", "idle"];          // le running vient de finir
  assert.strictEqual(doitSonder(etats), false);
});

// =====================================================================
// Mapping des codes d'erreur — exhaustif
// =====================================================================
const CATALOGUE = [
  "OFFER_NOT_FOUND", "MASTER_INVALID", "OLLAMA_UNAVAILABLE",
  "EXTRACTION_FAILED", "MATCHING_EMPTY", "TYPST_FAILED", "INTERNAL_ERROR",
  "TEXT_MISSING", "JOB_NOT_FOUND", "PDF_UNAVAILABLE",
];

test("chaque code du catalogue a un message lisible", () => {
  for (const code of CATALOGUE) {
    const msg = messageErreur(code);
    assert.ok(msg && msg.length > 15, `${code} : message trop court`);
    assert.ok(!msg.includes(code), `${code} : le code brut est affiché`);
    assert.ok(!msg.includes("%CODE%"), `${code} : message non substitué`);
  }
});

test("la table de messages couvre exactement le catalogue", () => {
  assert.deepStrictEqual(Object.keys(MESSAGES_ERREUR).sort(), [...CATALOGUE].sort());
});

test("un code inconnu DÉGRADE au lieu de lever", () => {
  const msg = messageErreur("CODE_DU_FUTUR");
  assert.match(msg, /CODE_DU_FUTUR/);
  assert.doesNotMatch(msg, /%CODE%/);
});

test("l'absence de code reste affichable", () => {
  assert.ok(messageErreur(null).length > 0);
  assert.ok(messageErreur(undefined).length > 0);
});

// =====================================================================
// Traduction depuis la réponse de /api/cv/states
// =====================================================================
test("une offre absente de la réponse est idle", () => {
  assert.strictEqual(etatDepuis(undefined), "idle");
  assert.strictEqual(etatDepuis(null), "idle");
});

test("les statuts de job se traduisent tels quels", () => {
  assert.strictEqual(etatDepuis({ status: "pending" }), "pending");
  assert.strictEqual(etatDepuis({ status: "running" }), "running");
  assert.strictEqual(etatDepuis({ status: "failed" }), "failed");
  assert.strictEqual(etatDepuis({ status: "text_missing" }), "text_missing");
});

test("un job done SANS pdf_url est un échec, pas un succès", () => {
  // Le job dit `done` mais la couche HTTP n'a pas de PDF à servir :
  // proposer « Télécharger » mènerait à un 404.
  assert.strictEqual(etatDepuis({ status: "done", pdf_url: null }), "failed");
  assert.strictEqual(
    etatDepuis({ status: "done", pdf_url: "/api/jobs/1/download" }), "done");
});

test("un statut inconnu lève", () => {
  assert.throws(() => etatDepuis({ status: "zombie" }), /statut de job inconnu/);
});
