from __future__ import annotations

import os
import re
import time
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager

import pandas as pd
import streamlit as st

# ---------- CONFIG ----------
APP_TITLE = "RESOLVE — Demande de kits"
DATA_DIR = "data"
XLSX_PATH = os.path.join(DATA_DIR, "resolve_inscriptions.xlsx")

ADMIN_PASSWORD = "SeeALL"  # demandé par toi

COLUMNS = [
    "timestamp_utc",
    "prenom",
    "nom",
    "email",
    "telephone",
    "clinique",
    "adresse_clinique",
    "diff_procedure_diag",  # ✅ réponse libre visible uniquement dans l'espace admin
]

# ---------- FILE LOCK (Linux/macOS) ----------
@contextmanager
def file_lock(lock_path: str, timeout_s: float = 10.0):
    """
    Simple lock via flock (ok sur Streamlit Cloud / Linux).
    Sur Windows (sans msvcrt), on fait best-effort (pas de lock strict).
    """
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    start = time.time()
    f = open(lock_path, "w", encoding="utf-8")
    try:
        try:
            import fcntl  # POSIX only

            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() - start > timeout_s:
                        raise TimeoutError("Impossible d'obtenir le verrou fichier (timeout).")
                    time.sleep(0.05)
        except Exception:
            # Fallback (Windows ou environnement sans fcntl) -> best effort
            pass

        yield
    finally:
        try:
            import fcntl  # POSIX only

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def is_valid_email(email: str) -> bool:
    # validation simple (suffisante pour formulaire)
    pattern = r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$"
    return bool(re.match(pattern, email.strip(), flags=re.IGNORECASE))


def is_valid_phone(phone: str) -> bool:
    # accepte +33, 0X..., espaces, points, tirets
    cleaned = re.sub(r"[^\d+]", "", phone)
    # minimum 10 chiffres si FR typique ; on reste permissif
    digits = re.sub(r"\D", "", cleaned)
    return len(digits) >= 10


def ensure_store_exists():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(XLSX_PATH):
        df = pd.DataFrame(columns=COLUMNS)
        df.to_excel(XLSX_PATH, index=False, engine="openpyxl")


def append_row_to_xlsx(row: dict):
    ensure_store_exists()
    lock_path = XLSX_PATH + ".lock"

    with file_lock(lock_path):
        # relire au moment d'écrire pour éviter pertes en concurrence
        try:
            df = pd.read_excel(XLSX_PATH, engine="openpyxl")
        except Exception:
            df = pd.DataFrame(columns=COLUMNS)

        for c in COLUMNS:
            if c not in df.columns:
                df[c] = None

        df = pd.concat([df, pd.DataFrame([row], columns=COLUMNS)], ignore_index=True)

        # écriture atomique (temp puis replace)
        tmp_dir = os.path.dirname(XLSX_PATH) or "."
        fd, tmp_path = tempfile.mkstemp(prefix="resolve_", suffix=".xlsx", dir=tmp_dir)
        os.close(fd)
        df.to_excel(tmp_path, index=False, engine="openpyxl")
        os.replace(tmp_path, XLSX_PATH)


def load_xlsx() -> pd.DataFrame:
    ensure_store_exists()
    try:
        df = pd.read_excel(XLSX_PATH, engine="openpyxl")
    except Exception:
        df = pd.DataFrame(columns=COLUMNS)

    # tri du plus récent au plus ancien
    if "timestamp_utc" in df.columns:
        df["timestamp_utc"] = df["timestamp_utc"].astype(str)
    return df


# ---------- UI ----------
st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧪",
    layout="wide",
)

CUSTOM_CSS = """
<style>
/* Fond + typographie */
.main {
  background: radial-gradient(1200px 600px at 15% 10%, rgba(110, 231, 183, 0.14), rgba(255,255,255,0)),
              radial-gradient(1200px 600px at 85% 10%, rgba(59, 130, 246, 0.14), rgba(255,255,255,0));
}
.block-container { padding-top: 2.2rem; padding-bottom: 2.0rem; }

/* Hero */
.hero {
  border-radius: 20px;
  padding: 22px 24px;
  border: 1px solid rgba(0,0,0,0.08);
  background: rgba(255,255,255,0.75);
  backdrop-filter: blur(8px);
  box-shadow: 0 10px 30px rgba(0,0,0,0.06);
}
.hero h1 { margin: 0; font-size: 32px; letter-spacing: -0.02em; }
.hero p { margin: 6px 0 0 0; color: rgba(0,0,0,0.70); font-size: 14px; }

/* Cartes */
.card {
  border-radius: 20px;
  padding: 18px 18px 8px 18px;
  border: 1px solid rgba(0,0,0,0.08);
  background: rgba(255,255,255,0.80);
  backdrop-filter: blur(8px);
  box-shadow: 0 10px 25px rgba(0,0,0,0.05);
}
.card h3 { margin: 0 0 10px 0; font-size: 18px; }

/* Bouton Streamlit */
div.stButton > button {
  width: 100%;
  border-radius: 14px !important;
  padding: 0.85rem 1.0rem !important;
  font-weight: 700 !important;
  border: 1px solid rgba(0,0,0,0.10) !important;
  box-shadow: 0 10px 24px rgba(0,0,0,0.08);
}
div.stButton > button:hover {
  transform: translateY(-1px);
}

/* Inputs un peu plus doux */
div[data-baseweb="input"] > div,
div[data-baseweb="textarea"] > div {
  border-radius: 14px !important;
}

/* Petit séparateur */
.hr {
  height: 1px; border: 0; background: rgba(0,0,0,0.08);
  margin: 14px 0 0 0;
}
.small-note { color: rgba(0,0,0,0.60); font-size: 12px; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

st.markdown(
    f"""
<div class="hero">
  <h1>🧪 {APP_TITLE}</h1>
  <p>Renseignez vos informations pour recevoir vos kits. Une fois les informations validées, votre demande est enregistrée et vos kits vous sont envoyés.</p>
  <hr class="hr" />
  <p class="small-note">Les champs marqués * sont obligatoires.</p>
</div>
""",
    unsafe_allow_html=True,
)

st.write("")

# ---------- FORM ----------
left, right = st.columns([1.2, 0.8], gap="large")

with left:
    st.markdown(
        '<div class="card"><h3>📝 Formulaire d’inscription - A destination des vétérinaires uniquement</h3>',
        unsafe_allow_html=True,
    )

    with st.form("resolve_form", clear_on_submit=False):
        c1, c2 = st.columns(2, gap="medium")
        with c1:
            prenom = st.text_input("Prénom *", placeholder="Ex : Marie")
        with c2:
            nom = st.text_input("Nom *", placeholder="Ex : Dupont")

        c3, c4 = st.columns(2, gap="medium")
        with c3:
            email = st.text_input("Mail *", placeholder="ex : marie.dupont@clinique.fr")
        with c4:
            telephone = st.text_input("Numéro de téléphone *", placeholder="ex : +33 6 12 34 56 78")

        clinique = st.text_input("Nom de clinique *", placeholder="Ex : Clinique Vétérinaire des Yvelines")
        adresse = st.text_area(
            "Adresse de la clinique où vous souhaitez recevoir les kits RESOLVE *",
            placeholder="N° et rue, code postal, ville",
            height=110,
        )

        # ✅ Nouvelle question (réponse libre) — stockée dans l'Excel, visible uniquement côté admin
        diff_procedure_diag = st.text_area(
            "En tant que vétérinaire de terrain, quelles sont les différences entre la procédure diagnostique expliquée dans la vidéo et la procédure diagnostique Lyme que vous utilisez habituellement",
            placeholder="Votre réponse…",
            height=140,
        )

        submitted = st.form_submit_button("✅ Valider la demande")

    st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        prenom_n = normalize_spaces(prenom)
        nom_n = normalize_spaces(nom)
        email_n = normalize_spaces(email).lower()
        tel_n = normalize_spaces(telephone)
        clinique_n = normalize_spaces(clinique)
        adresse_n = normalize_spaces(adresse)
        diff_diag_n = normalize_spaces(diff_procedure_diag)

        missing = []
        if not prenom_n:
            missing.append("Prénom")
        if not nom_n:
            missing.append("Nom")
        if not email_n:
            missing.append("Mail")
        if not tel_n:
            missing.append("Téléphone")
        if not clinique_n:
            missing.append("Nom de clinique")
        if not adresse_n:
            missing.append("Adresse de la clinique")

        if missing:
            st.error("Merci de compléter tous les champs obligatoires : " + ", ".join(missing))
        else:
            if not is_valid_email(email_n):
                st.error("Le format du mail ne semble pas valide.")
            elif not is_valid_phone(tel_n):
                st.error("Le numéro de téléphone ne semble pas valide (au moins 10 chiffres).")
            else:
                row = {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "prenom": prenom_n,
                    "nom": nom_n,
                    "email": email_n,
                    "telephone": tel_n,
                    "clinique": clinique_n,
                    "adresse_clinique": adresse_n,
                    "diff_procedure_diag": diff_diag_n,
                }
                try:
                    append_row_to_xlsx(row)
                    st.success("✅ Demande enregistrée !")
                    st.info("🙏 Merci pour votre participation à l’étude RESOLVE. Votre demande a bien été prise en compte.")
                except Exception as e:
                    st.error("Une erreur est survenue lors de l’enregistrement. Merci de réessayer.")
                    st.caption(f"Détail technique (admin): {e}")

with right:
    st.markdown('<div class="card"><h3>ℹ️ À propos</h3>', unsafe_allow_html=True)
    st.write(
        "Cette page permet de centraliser les demandes de kits RESOLVE.\n\n"
        "- Remplissez le formulaire\n"
        "- Cliquez sur **Valider la demande**\n"
        "- Vous recevrez vos kits RESOLVE dans les prochains jours"
    )
    st.caption("Astuce : utilisez une adresse mail professionnelle de la clinique si possible.")
    st.markdown("</div>", unsafe_allow_html=True)

st.write("")

# ---------- ADMIN (PASSWORD-PROTECTED) ----------
with st.expander("🔒 Espace administrateur (réservé)"):
    st.markdown(
        "<div class='card'><h3>📊 Inscriptions enregistrées</h3></div>",
        unsafe_allow_html=True,
    )
    admin_pass = st.text_input("Mot de passe", type="password", placeholder="Entrer le mot de passe admin")

    if admin_pass:
        if admin_pass == ADMIN_PASSWORD:
            df = load_xlsx()
            st.success("Accès autorisé ✅")

            # KPIs
            total = len(df)
            st.metric("Nombre total d'inscriptions", total)

            # Affichage tableau (inclut la colonne diff_procedure_diag)
            st.dataframe(df, use_container_width=True, height=360)

            # Bouton de téléchargement
            with open(XLSX_PATH, "rb") as f:
                st.download_button(
                    label="⬇️ Télécharger le fichier Excel",
                    data=f,
                    file_name="resolve_inscriptions.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

            st.caption("Le fichier est stocké côté serveur (non visible publiquement).")
        else:
            st.error("Mot de passe incorrect.")
