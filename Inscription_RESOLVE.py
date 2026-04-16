from __future__ import annotations

import base64
import io
import mimetypes
import os
import re
import time
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Dict, List
from xml.sax.saxutils import escape
import smtplib
from email.message import EmailMessage

import pandas as pd
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ==============================================================================
# CONFIG
# ==============================================================================
APP_TITLE = "RESOLVE — Étude prospective"
DATA_DIR = "data"
XLSX_PATH = os.path.join(DATA_DIR, "resolve_study_data.xlsx")
ADMIN_PASSWORD = "SeeALL"

DETENTEUR_IMAGE_PATH = "https://raw.githubusercontent.com/QuentinLamboley/kitresolve/main/detenteurs.png"
VETERINAIRE_IMAGE_PATH = "https://raw.githubusercontent.com/QuentinLamboley/kitresolve/main/veterinairesequin.png"

SHEET_SUBMISSIONS = "submissions"
SHEET_SAMPLES = "sample_locations"

SUBMISSION_COLUMNS = [
    "submission_id",
    "timestamp_utc",
    "profil",
    "statut_dossier",
    "contact_prenom",
    "contact_nom",
    "contact_email",
    "contact_telephone",
    "contact_structure",
    "contact_adresse",
    "contact_ville",
    "contact_code_postal",
    "contact_region",
    "proprietaire_prenom",
    "proprietaire_nom",
    "proprietaire_email",
    "proprietaire_telephone",
    "veterinaire_prenom",
    "veterinaire_nom",
    "veterinaire_email",
    "veterinaire_telephone",
    "veterinaire_structure",
    "veterinaire_adresse",
    "veterinaire_ville",
    "veterinaire_code_postal",
    "veterinaire_region",
    "cheval_nom",
    "cheval_age",
    "cheval_sexe",
    "cheval_race",
    "cheval_commune",
    "cheval_departement",
    "cheval_region",
    "cheval_lieu_detention_coordonnees",
    "contact_regulier_tiques_vegetation",
    "aucune_maladie_precise_connue",
    "signes_cliniques_evocateurs",
    "signes_cliniques_generaux",
    "signes_cliniques_articulaires",
    "signes_cliniques_oculaires",
    "signes_cliniques_cutanes",
    "accord_prelevement_liquide_synovial",
    "accord_prelevement_humeur_aqueuse",
    "accord_prelevement_cutane",
    "resume_signes_cliniques",
    "accord_bilan_sanguin_complet",
    "accord_test_negatif_piroplasmose",
    "accord_test_negatif_ehrlichiose",
    "contexte_large",
    "souhaite_etre_recontacte",
    "consentement_contact",
    "consentement_donnees",
    "a_besoin_kit_resolve",
    "nb_chevaux_concernes",
    "procedure_diagnostique_habituelle",
    "notes_admin",
]

SAMPLE_COLUMNS = [
    "sample_id",
    "timestamp_utc",
    "submission_id",
    "cheval_nom",
    "type_prelevement",
    "date_prelevement",
    "statut_prelevement",
    "adresse_site",
    "ville_site",
    "code_postal_site",
    "region_site",
    "latitude",
    "longitude",
    "commentaire",
]

ROLE_OPTIONS = {
    "detenteur": {
        "label": "Je suis détenteur",
        "emoji": "🐎",
        "subtitle": "Je souhaite signaler un cheval potentiellement éligible et transmettre les coordonnées du vétérinaire.",
        "image_path": DETENTEUR_IMAGE_PATH,
    },
    "veterinaire": {
        "label": "Je suis vétérinaire",
        "emoji": "🩺",
        "subtitle": "Je souhaite participer à l’étude, recevoir un kit RESOLVE et inclure un ou plusieurs chevaux.",
        "image_path": VETERINAIRE_IMAGE_PATH,
    },
}

DEFAULT_STATUS = "nouvelle_demande"

# ==============================================================================
# HELPERS
# ==============================================================================
@contextmanager
def file_lock(lock_path: str, timeout_s: float = 12.0):
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    start = time.time()
    f = open(lock_path, "w", encoding="utf-8")
    try:
        try:
            import fcntl

            while True:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() - start > timeout_s:
                        raise TimeoutError("Impossible d'obtenir le verrou fichier.")
                    time.sleep(0.05)
        except Exception:
            pass

        yield
    finally:
        try:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_email(email: str) -> str:
    return normalize_spaces(email).lower()


def normalize_phone(phone: str) -> str:
    return normalize_spaces(phone)


def is_valid_email(email: str) -> bool:
    pattern = r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$"
    return bool(re.match(pattern, (email or "").strip(), flags=re.IGNORECASE))


def is_valid_phone(phone: str) -> bool:
    cleaned = re.sub(r"[^\d+]", "", phone or "")
    digits = re.sub(r"\D", "", cleaned)
    return len(digits) >= 10


def safe_float(v):
    if v is None:
        return None
    try:
        if str(v).strip() == "":
            return None
        return float(v)
    except Exception:
        return None


def is_valid_lat_lon(lat, lon) -> bool:
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def make_submission_id() -> str:
    return f"RESOLVE-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"


def make_sample_id() -> str:
    return f"SAMPLE-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"


def ensure_store_exists():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(XLSX_PATH):
        with pd.ExcelWriter(XLSX_PATH, engine="openpyxl") as writer:
            pd.DataFrame(columns=SUBMISSION_COLUMNS).to_excel(
                writer, sheet_name=SHEET_SUBMISSIONS, index=False
            )
            pd.DataFrame(columns=SAMPLE_COLUMNS).to_excel(
                writer, sheet_name=SHEET_SAMPLES, index=False
            )


def load_sheet(sheet_name: str, expected_columns: List[str]) -> pd.DataFrame:
    ensure_store_exists()
    try:
        df = pd.read_excel(XLSX_PATH, sheet_name=sheet_name, engine="openpyxl")
    except Exception:
        df = pd.DataFrame(columns=expected_columns)

    for c in expected_columns:
        if c not in df.columns:
            df[c] = None

    df = df[expected_columns]
    return df


def write_all_sheets_atomic(sheets: Dict[str, pd.DataFrame]):
    tmp_dir = os.path.dirname(XLSX_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="resolve_", suffix=".xlsx", dir=tmp_dir)
    os.close(fd)
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        os.replace(tmp_path, XLSX_PATH)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def append_submission(rows: List[dict]):
    ensure_store_exists()
    lock_path = XLSX_PATH + ".lock"
    with file_lock(lock_path):
        submissions = load_sheet(SHEET_SUBMISSIONS, SUBMISSION_COLUMNS)
        samples = load_sheet(SHEET_SAMPLES, SAMPLE_COLUMNS)

        for c in SUBMISSION_COLUMNS:
            if c not in submissions.columns:
                submissions[c] = None

        submissions = pd.concat(
            [submissions, pd.DataFrame(rows, columns=SUBMISSION_COLUMNS)],
            ignore_index=True,
        )
        write_all_sheets_atomic(
            {
                SHEET_SUBMISSIONS: submissions,
                SHEET_SAMPLES: samples,
            }
        )


def append_sample_location(row: dict):
    ensure_store_exists()
    lock_path = XLSX_PATH + ".lock"
    with file_lock(lock_path):
        submissions = load_sheet(SHEET_SUBMISSIONS, SUBMISSION_COLUMNS)
        samples = load_sheet(SHEET_SAMPLES, SAMPLE_COLUMNS)

        for c in SAMPLE_COLUMNS:
            if c not in samples.columns:
                samples[c] = None

        samples = pd.concat(
            [samples, pd.DataFrame([row], columns=SAMPLE_COLUMNS)],
            ignore_index=True,
        )
        write_all_sheets_atomic(
            {
                SHEET_SUBMISSIONS: submissions,
                SHEET_SAMPLES: samples,
            }
        )


def load_all_data():
    submissions = load_sheet(SHEET_SUBMISSIONS, SUBMISSION_COLUMNS)
    samples = load_sheet(SHEET_SAMPLES, SAMPLE_COLUMNS)

    if "timestamp_utc" in submissions.columns:
        submissions["timestamp_utc"] = submissions["timestamp_utc"].astype(str)
    if "timestamp_utc" in samples.columns:
        samples["timestamp_utc"] = samples["timestamp_utc"].astype(str)

    return submissions, samples


def filter_map_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=["lat", "lon", "label"])
    out = df.copy()
    out["latitude"] = pd.to_numeric(out["latitude"], errors="coerce")
    out["longitude"] = pd.to_numeric(out["longitude"], errors="coerce")
    out = out.dropna(subset=["latitude", "longitude"])
    out = out[(out["latitude"].between(-90, 90)) & (out["longitude"].between(-180, 180))]
    if out.empty:
        return pd.DataFrame(columns=["lat", "lon", "label"])
    out = out.rename(columns={"latitude": "lat", "longitude": "lon"})
    out["label"] = (
        out["cheval_nom"].fillna("").astype(str)
        + " — "
        + out["type_prelevement"].fillna("").astype(str)
        + " — "
        + out["statut_prelevement"].fillna("").astype(str)
    )
    return out[["lat", "lon", "label"]]


def serialize_bool(v) -> str:
    return "oui" if bool(v) else "non"


def bool_to_text(v: bool) -> str:
    return "Oui" if bool(v) else "Non"


def validate_submission(payload: dict) -> List[str]:
    errors = []

    profil = payload.get("profil")
    if profil not in {"detenteur", "veterinaire"}:
        errors.append("Merci de sélectionner un profil.")

    required_common = [
        ("contact_prenom", "Prénom"),
        ("contact_nom", "Nom"),
        ("contact_email", "Mail"),
        ("contact_telephone", "Téléphone"),
    ]
    for key, label in required_common:
        if not normalize_spaces(str(payload.get(key, ""))):
            errors.append(f"{label} obligatoire.")

    email = normalize_email(payload.get("contact_email", ""))
    tel = normalize_phone(payload.get("contact_telephone", ""))
    if email and not is_valid_email(email):
        errors.append("Le format du mail principal ne semble pas valide.")
    if tel and not is_valid_phone(tel):
        errors.append("Le numéro de téléphone principal ne semble pas valide.")

    if profil == "veterinaire":
        vet_required = [
            ("contact_adresse", "Adresse de la structure vétérinaire"),
            ("contact_structure", "Nom de la structure vétérinaire"),
        ]
        for key, label in vet_required:
            if not normalize_spaces(str(payload.get(key, ""))):
                errors.append(f"{label} obligatoire.")
    elif profil == "detenteur":
        owner_required = [
            ("veterinaire_prenom", "Prénom du vétérinaire"),
            ("veterinaire_nom", "Nom du vétérinaire"),
            ("veterinaire_email", "Mail du vétérinaire"),
            ("veterinaire_telephone", "Téléphone du vétérinaire"),
            ("veterinaire_structure", "Structure du vétérinaire"),
        ]
        for key, label in owner_required:
            if not normalize_spaces(str(payload.get(key, ""))):
                errors.append(f"{label} obligatoire.")
        vet_email = normalize_email(payload.get("veterinaire_email", ""))
        vet_tel = normalize_phone(payload.get("veterinaire_telephone", ""))
        if vet_email and not is_valid_email(vet_email):
            errors.append("Le format du mail du vétérinaire ne semble pas valide.")
        if vet_tel and not is_valid_phone(vet_tel):
            errors.append("Le téléphone du vétérinaire ne semble pas valide.")

    horses = payload.get("horses", [])
    if not horses:
        errors.append("Merci de renseigner au moins un cheval.")

    for idx, horse in enumerate(horses, start=1):
        prefix = f"Cheval {idx}"
        if not normalize_spaces(horse.get("cheval_nom", "")):
            errors.append(f"{prefix} : nom du cheval obligatoire.")
        if not normalize_spaces(horse.get("cheval_lieu_detention_coordonnees", "")):
            errors.append(f"{prefix} : coordonnées du lieu de détention obligatoires.")
        if not horse.get("contact_regulier_tiques_vegetation", False):
            errors.append(f"{prefix} : le critère « contact régulier avec tiques / végétation » doit être coché.")
        if not horse.get("aucune_maladie_precise_connue", False):
            errors.append(f"{prefix} : le critère « aucune maladie précise connue à ce jour » doit être coché.")
        if not horse.get("signes_cliniques_evocateurs", False):
            errors.append(f"{prefix} : le critère « signes cliniques évocateurs » doit être coché.")
        if not normalize_spaces(horse.get("resume_signes_cliniques", "")):
            errors.append(f"{prefix} : merci de renseigner les signes cliniques observés.")
        if not normalize_spaces(horse.get("contexte_large", "")):
            errors.append(f"{prefix} : merci de renseigner le contexte au sens large.")

    if not payload.get("consentement_contact", False):
        errors.append("Le consentement de contact est requis.")
    if not payload.get("consentement_donnees", False):
        errors.append("Le consentement de traitement des données est requis.")

    return errors


def build_submission_rows(payload: dict) -> List[dict]:
    profil = payload["profil"]
    base_submission_id = make_submission_id()
    horses = payload["horses"]

    owner_first = normalize_spaces(payload.get("contact_prenom", "")) if profil == "detenteur" else ""
    owner_last = normalize_spaces(payload.get("contact_nom", "")) if profil == "detenteur" else ""
    owner_email = normalize_email(payload.get("contact_email", "")) if profil == "detenteur" else ""
    owner_tel = normalize_phone(payload.get("contact_telephone", "")) if profil == "detenteur" else ""

    vet_first = normalize_spaces(payload.get("contact_prenom", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_prenom", ""))
    vet_last = normalize_spaces(payload.get("contact_nom", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_nom", ""))
    vet_email = normalize_email(payload.get("contact_email", "")) if profil == "veterinaire" else normalize_email(payload.get("veterinaire_email", ""))
    vet_tel = normalize_phone(payload.get("contact_telephone", "")) if profil == "veterinaire" else normalize_phone(payload.get("veterinaire_telephone", ""))

    vet_structure = normalize_spaces(payload.get("contact_structure", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_structure", ""))
    vet_adresse = normalize_spaces(payload.get("contact_adresse", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_adresse", ""))
    vet_ville = normalize_spaces(payload.get("contact_ville", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_ville", ""))
    vet_cp = normalize_spaces(payload.get("contact_code_postal", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_code_postal", ""))
    vet_region = normalize_spaces(payload.get("contact_region", "")) if profil == "veterinaire" else normalize_spaces(payload.get("veterinaire_region", ""))

    rows = []
    total_horses = len(horses)

    for idx, horse in enumerate(horses, start=1):
        row = {
            "submission_id": f"{base_submission_id}-H{idx:02d}",
            "timestamp_utc": utc_now_iso(),
            "profil": profil,
            "statut_dossier": DEFAULT_STATUS,
            "contact_prenom": normalize_spaces(payload.get("contact_prenom", "")),
            "contact_nom": normalize_spaces(payload.get("contact_nom", "")),
            "contact_email": normalize_email(payload.get("contact_email", "")),
            "contact_telephone": normalize_phone(payload.get("contact_telephone", "")),
            "contact_structure": normalize_spaces(payload.get("contact_structure", "")),
            "contact_adresse": normalize_spaces(payload.get("contact_adresse", "")),
            "contact_ville": normalize_spaces(payload.get("contact_ville", "")),
            "contact_code_postal": normalize_spaces(payload.get("contact_code_postal", "")),
            "contact_region": normalize_spaces(payload.get("contact_region", "")),
            "proprietaire_prenom": owner_first,
            "proprietaire_nom": owner_last,
            "proprietaire_email": owner_email,
            "proprietaire_telephone": owner_tel,
            "veterinaire_prenom": vet_first,
            "veterinaire_nom": vet_last,
            "veterinaire_email": vet_email,
            "veterinaire_telephone": vet_tel,
            "veterinaire_structure": vet_structure,
            "veterinaire_adresse": vet_adresse,
            "veterinaire_ville": vet_ville,
            "veterinaire_code_postal": vet_cp,
            "veterinaire_region": vet_region,
            "cheval_nom": normalize_spaces(horse.get("cheval_nom", "")),
            "cheval_age": normalize_spaces(horse.get("cheval_age", "")),
            "cheval_sexe": normalize_spaces(horse.get("cheval_sexe", "")),
            "cheval_race": normalize_spaces(horse.get("cheval_race", "")),
            "cheval_commune": normalize_spaces(horse.get("cheval_commune", "")),
            "cheval_departement": normalize_spaces(horse.get("cheval_departement", "")),
            "cheval_region": "",
            "cheval_lieu_detention_coordonnees": normalize_spaces(horse.get("cheval_lieu_detention_coordonnees", "")),
            "contact_regulier_tiques_vegetation": serialize_bool(horse.get("contact_regulier_tiques_vegetation", False)),
            "aucune_maladie_precise_connue": serialize_bool(horse.get("aucune_maladie_precise_connue", False)),
            "signes_cliniques_evocateurs": serialize_bool(horse.get("signes_cliniques_evocateurs", False)),
            "signes_cliniques_generaux": serialize_bool(horse.get("signes_cliniques_generaux", False)),
            "signes_cliniques_articulaires": serialize_bool(horse.get("signes_cliniques_articulaires", False)),
            "signes_cliniques_oculaires": serialize_bool(horse.get("signes_cliniques_oculaires", False)),
            "signes_cliniques_cutanes": serialize_bool(horse.get("signes_cliniques_cutanes", False)),
            "accord_prelevement_liquide_synovial": serialize_bool(horse.get("accord_prelevement_liquide_synovial", False)),
            "accord_prelevement_humeur_aqueuse": serialize_bool(horse.get("accord_prelevement_humeur_aqueuse", False)),
            "accord_prelevement_cutane": serialize_bool(horse.get("accord_prelevement_cutane", False)),
            "resume_signes_cliniques": normalize_spaces(horse.get("resume_signes_cliniques", "")),
            "accord_bilan_sanguin_complet": serialize_bool(horse.get("accord_bilan_sanguin_complet", False)),
            "accord_test_negatif_piroplasmose": serialize_bool(horse.get("accord_test_negatif_piroplasmose", False)),
            "accord_test_negatif_ehrlichiose": serialize_bool(horse.get("accord_test_negatif_ehrlichiose", False)),
            "contexte_large": normalize_spaces(horse.get("contexte_large", "")),
            "souhaite_etre_recontacte": serialize_bool(payload.get("souhaite_etre_recontacte", False)),
            "consentement_contact": serialize_bool(payload.get("consentement_contact", False)),
            "consentement_donnees": serialize_bool(payload.get("consentement_donnees", False)),
            "a_besoin_kit_resolve": serialize_bool(payload.get("a_besoin_kit_resolve", True)),
            "nb_chevaux_concernes": str(total_horses),
            "procedure_diagnostique_habituelle": normalize_spaces(payload.get("procedure_diagnostique_habituelle", "")),
            "notes_admin": "",
        }
        rows.append(row)

    return rows


def get_download_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()

def send_pdf_email(
    pdf_bytes: bytes,
    pdf_filename: str,
    to_email: str,
    subject: str,
    body: str,
):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = st.secrets["SMTP_USER"]
    msg["To"] = to_email
    msg.set_content(body)

    msg.add_attachment(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        filename=pdf_filename,
    )

    with smtplib.SMTP_SSL(
        st.secrets["SMTP_HOST"],
        int(st.secrets["SMTP_PORT"]),
    ) as smtp:
        smtp.login(
            st.secrets["SMTP_USER"],
            st.secrets["SMTP_PASSWORD"],
        )
        smtp.send_message(msg)        

def image_to_data_uri(path: str) -> str:
    if not path:
        return ""

    if path.startswith("http://") or path.startswith("https://"):
        try:
            import requests

            response = requests.get(path, timeout=10)
            response.raise_for_status()
            content = response.content
            mime_type = response.headers.get("Content-Type", "").split(";")[0] or "image/png"
            encoded = base64.b64encode(content).decode("utf-8")
            return f"data:{mime_type};base64,{encoded}"
        except Exception:
            return ""

    if not os.path.exists(path):
        return ""

    mime_type = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def render_role_visual(image_path: str, fallback_emoji: str, fallback_title: str):
    img_uri = image_to_data_uri(image_path)
    if img_uri:
        st.markdown(
            f"""
<div class="role-visual-frame">
  <div class="role-visual-card">
    <img src="{img_uri}" alt="{fallback_title}">
    <div class="role-visual-shine"></div>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
<div class="role-visual-frame">
  <div class="role-visual-card role-visual-fallback">
    <div class="role-fallback-inner">
      <div class="role-fallback-emoji">{fallback_emoji}</div>
      <div class="role-fallback-title">{fallback_title}</div>
      <div class="role-fallback-note">Image introuvable au chemin indiqué</div>
    </div>
    <div class="role-visual-shine"></div>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )


def go_home():
    st.session_state.current_view = "home"
    st.session_state.selected_role = None


def go_form(role: str):
    st.session_state.selected_role = role
    st.session_state.current_view = "form"


def make_pdf_filename(role: str) -> str:
    role_part = "detenteur" if role == "detenteur" else "veterinaire"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"resolve_{role_part}_{ts}.pdf"


def add_pdf_table(story, data, col_widths=None):
    table = Table(data, colWidths=col_widths, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeaf6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#10243e")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEADING", (0, 0), (-1, -1), 12),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#aabbd0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 0.35 * cm))


def create_submission_pdf_bytes(payload: dict, rows: List[dict]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.4 * cm,
        leftMargin=1.4 * cm,
        topMargin=1.3 * cm,
        bottomMargin=1.3 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ResolveTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        alignment=TA_LEFT,
        textColor=colors.HexColor("#10243e"),
        spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "ResolveSection",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#16375b"),
        spaceBefore=8,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "ResolveBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.3,
        leading=12,
        alignment=TA_LEFT,
        textColor=colors.black,
        spaceAfter=4,
    )

    story = []

    role_label = "Détenteur" if payload.get("profil") == "detenteur" else "Vétérinaire"
    story.append(Paragraph("RESOLVE — Formulaire de participation", title_style))
    story.append(
        Paragraph(
            f"<b>Profil :</b> {escape(role_label)}<br/>"
            f"<b>Date de génération :</b> {escape(datetime.now().strftime('%d/%m/%Y %H:%M:%S'))}<br/>"
            f"<b>Nombre de chevaux :</b> {escape(str(len(payload.get('horses', []))))}",
            body_style,
        )
    )
    story.append(Spacer(1, 0.2 * cm))

    story.append(Paragraph("Coordonnées du déclarant", section_style))
    add_pdf_table(
        story,
        [
            ["Champ", "Valeur"],
            ["Prénom", normalize_spaces(payload.get("contact_prenom", ""))],
            ["Nom", normalize_spaces(payload.get("contact_nom", ""))],
            ["Mail", normalize_email(payload.get("contact_email", ""))],
            ["Téléphone", normalize_phone(payload.get("contact_telephone", ""))],
            ["Structure", normalize_spaces(payload.get("contact_structure", ""))],
            ["Adresse", normalize_spaces(payload.get("contact_adresse", ""))],
            ["Ville", normalize_spaces(payload.get("contact_ville", ""))],
            ["Code postal", normalize_spaces(payload.get("contact_code_postal", ""))],
            ["Région", normalize_spaces(payload.get("contact_region", ""))],
        ],
        [5.2 * cm, 12.2 * cm],
    )

    if payload.get("profil") == "detenteur":
        story.append(Paragraph("Coordonnées du vétérinaire à contacter", section_style))
        add_pdf_table(
            story,
            [
                ["Champ", "Valeur"],
                ["Prénom", normalize_spaces(payload.get("veterinaire_prenom", ""))],
                ["Nom", normalize_spaces(payload.get("veterinaire_nom", ""))],
                ["Mail", normalize_email(payload.get("veterinaire_email", ""))],
                ["Téléphone", normalize_phone(payload.get("veterinaire_telephone", ""))],
                ["Structure", normalize_spaces(payload.get("veterinaire_structure", ""))],
                ["Adresse", normalize_spaces(payload.get("veterinaire_adresse", ""))],
                ["Ville", normalize_spaces(payload.get("veterinaire_ville", ""))],
                ["Code postal", normalize_spaces(payload.get("veterinaire_code_postal", ""))],
                ["Région", normalize_spaces(payload.get("veterinaire_region", ""))],
            ],
            [5.2 * cm, 12.2 * cm],
        )
    else:
        story.append(Paragraph("Procédure diagnostique habituelle", section_style))
        story.append(
            Paragraph(
                escape(normalize_spaces(payload.get("procedure_diagnostique_habituelle", "")) or "Non renseigné"),
                body_style,
            )
        )

    for idx, horse in enumerate(payload.get("horses", []), start=1):
        story.append(Paragraph(f"Cheval {idx}", section_style))

        categories = []
        if horse.get("signes_cliniques_generaux", False):
            categories.append("Signes cliniques généraux")
        if horse.get("signes_cliniques_articulaires", False):
            categories.append("Signes cliniques articulaires")
        if horse.get("signes_cliniques_oculaires", False):
            categories.append("Signes cliniques oculaires")
        if horse.get("signes_cliniques_cutanes", False):
            categories.append("Signes cliniques cutanés")

        add_pdf_table(
            story,
            [
                ["Champ", "Valeur"],
                ["Nom du cheval", normalize_spaces(horse.get("cheval_nom", ""))],
                ["Âge", normalize_spaces(horse.get("cheval_age", ""))],
                ["Sexe", normalize_spaces(horse.get("cheval_sexe", ""))],
                ["Race", normalize_spaces(horse.get("cheval_race", ""))],
                ["Commune de détention", normalize_spaces(horse.get("cheval_commune", ""))],
                ["Département", normalize_spaces(horse.get("cheval_departement", ""))],
                ["Coordonnées du lieu de détention", normalize_spaces(horse.get("cheval_lieu_detention_coordonnees", ""))],
                ["Contact régulier avec tiques / végétation", bool_to_text(horse.get("contact_regulier_tiques_vegetation", False))],
                ["Aucune maladie précise connue à ce jour", bool_to_text(horse.get("aucune_maladie_precise_connue", False))],
                ["Présence de signes cliniques évocateurs", bool_to_text(horse.get("signes_cliniques_evocateurs", False))],
                ["Types de signes cliniques cochés", ", ".join(categories) if categories else "Aucun type coché"],
                ["Accord prélèvement liquide synovial", bool_to_text(horse.get("accord_prelevement_liquide_synovial", False))],
                ["Accord prélèvement d’humeur aqueuse", bool_to_text(horse.get("accord_prelevement_humeur_aqueuse", False))],
                ["Accord prélèvement cutané", bool_to_text(horse.get("accord_prelevement_cutane", False))],
                ["Description des signes cliniques", normalize_spaces(horse.get("resume_signes_cliniques", ""))],
                ["Accord bilan sanguin complet", bool_to_text(horse.get("accord_bilan_sanguin_complet", False))],
                ["Accord test négatif piroplasmose", bool_to_text(horse.get("accord_test_negatif_piroplasmose", False))],
                ["Accord test négatif ehrlichiose", bool_to_text(horse.get("accord_test_negatif_ehrlichiose", False))],
                ["Contexte au sens large", normalize_spaces(horse.get("contexte_large", ""))],
            ],
            [5.2 * cm, 12.2 * cm],
        )

    story.append(Paragraph("Logistique et consentements", section_style))
    add_pdf_table(
        story,
        [
            ["Champ", "Valeur"],
            ["Souhaite être recontacté", bool_to_text(payload.get("souhaite_etre_recontacte", False))],
            ["Consentement de contact", bool_to_text(payload.get("consentement_contact", False))],
            ["Consentement de traitement des données", bool_to_text(payload.get("consentement_donnees", False))],
            ["Besoin d’un kit RESOLVE", bool_to_text(payload.get("a_besoin_kit_resolve", False))],
            ["Procédure diagnostique habituelle", normalize_spaces(payload.get("procedure_diagnostique_habituelle", "")) or "Non renseigné"],
        ],
        [5.2 * cm, 12.2 * cm],
    )

    if rows:
        story.append(Paragraph("Identifiants de soumission générés", section_style))
        add_pdf_table(
            story,
            [["ID de soumission"]] + [[row["submission_id"]] for row in rows],
            [17.4 * cm],
        )

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


# ==============================================================================
# PAGE CONFIG + STYLE
# ==============================================================================
st.set_page_config(
    page_title=APP_TITLE,
    page_icon="🧬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if "selected_role" not in st.session_state:
    st.session_state.selected_role = None
if "horse_count" not in st.session_state:
    st.session_state.horse_count = 1
if "current_view" not in st.session_state:
    st.session_state.current_view = "home"
if "last_pdf_bytes" not in st.session_state:
    st.session_state.last_pdf_bytes = None
if "last_pdf_filename" not in st.session_state:
    st.session_state.last_pdf_filename = None

CUSTOM_CSS = """
<style>
:root{
  --bg:#050914;
  --bg2:#0a1020;
  --bg3:#101935;
  --card:rgba(12,18,36,0.58);
  --card-strong:rgba(14,22,44,0.78);
  --glass:rgba(255,255,255,0.06);
  --glass-2:rgba(255,255,255,0.11);
  --stroke:rgba(255,255,255,0.11);
  --stroke-strong:rgba(255,255,255,0.20);
  --text:#f8fbff;
  --muted:rgba(248,251,255,0.76);
  --muted-2:rgba(248,251,255,0.55);
  --green:#8bffe6;
  --cyan:#6be8ff;
  --blue:#8ab6ff;
  --purple:#9f8cff;
  --pink:#ff8fd6;
  --amber:#ffd66b;
  --danger:#ff8e8e;
}

html, body, [class*="css"]{
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
}

html{
  scroll-behavior:smooth;
}

body{
  color:var(--text);
}

.stApp{
  background:
    radial-gradient(1200px 720px at 8% 0%, rgba(34,211,238,0.16), transparent 56%),
    radial-gradient(1100px 680px at 96% 4%, rgba(59,130,246,0.20), transparent 52%),
    radial-gradient(900px 520px at 80% 92%, rgba(236,72,153,0.12), transparent 58%),
    radial-gradient(1000px 520px at 15% 90%, rgba(139,92,246,0.10), transparent 60%),
    linear-gradient(180deg, #040812 0%, #09101d 28%, #0b1222 58%, #0b1020 100%);
  color:var(--text);
  overflow-x:hidden;
}

.stApp:before{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  background:
    linear-gradient(115deg, rgba(255,255,255,0.02), transparent 28%, rgba(255,255,255,0.01) 54%, transparent 76%),
    radial-gradient(circle at 20% 30%, rgba(255,255,255,0.05) 0, transparent 1.2px),
    radial-gradient(circle at 80% 70%, rgba(255,255,255,0.04) 0, transparent 1.2px);
  background-size:auto, 26px 26px, 34px 34px;
  opacity:0.28;
}

.block-container{
  max-width:1360px;
  padding-top:1.15rem;
  padding-bottom:2.6rem;
}

[data-testid="stHeader"]{
  background:rgba(0,0,0,0);
}

section[data-testid="stSidebar"]{
  display:none;
}

.hero-shell{
  position:relative;
  overflow:hidden;
  border-radius:34px;
  border:1px solid var(--stroke);
  background:
    linear-gradient(140deg, rgba(255,255,255,0.11), rgba(255,255,255,0.03) 34%, rgba(255,255,255,0.015) 100%),
    linear-gradient(180deg, rgba(11,18,34,0.88), rgba(9,14,28,0.76));
  box-shadow:
    0 40px 110px rgba(0,0,0,0.42),
    inset 0 1px 0 rgba(255,255,255,0.10),
    inset 0 -1px 0 rgba(255,255,255,0.03);
  backdrop-filter: blur(22px) saturate(135%);
  -webkit-backdrop-filter: blur(22px) saturate(135%);
  padding: 38px 34px 32px 34px;
  margin-bottom: 22px;
  transform-style: preserve-3d;
}

.hero-shell:before{
  content:"";
  position:absolute;
  inset:-24%;
  background:
    radial-gradient(circle at 12% 16%, rgba(139,255,230,0.28), transparent 18%),
    radial-gradient(circle at 82% 18%, rgba(138,182,255,0.24), transparent 18%),
    radial-gradient(circle at 86% 80%, rgba(255,143,214,0.16), transparent 16%),
    radial-gradient(circle at 30% 85%, rgba(111,232,255,0.12), transparent 18%);
  filter: blur(28px);
  pointer-events:none;
}

.hero-shell:after{
  content:"";
  position:absolute;
  inset:auto -6% -45% auto;
  width:440px;
  height:440px;
  border-radius:50%;
  background:radial-gradient(circle, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0.03) 34%, transparent 68%);
  filter:blur(20px);
  pointer-events:none;
}

.kicker{
  position:relative;
  z-index:2;
  display:inline-flex;
  align-items:center;
  gap:8px;
  font-size:12px;
  letter-spacing:.16em;
  text-transform:uppercase;
  color:#dcfff5;
  border:1px solid rgba(139,255,230,0.20);
  background:linear-gradient(135deg, rgba(139,255,230,0.10), rgba(138,182,255,0.08));
  padding:9px 14px;
  border-radius:999px;
  box-shadow:
    0 10px 26px rgba(0,0,0,0.18),
    inset 0 1px 0 rgba(255,255,255,0.12);
}

.hero-title{
  position:relative;
  z-index:2;
  margin:16px 0 12px 0;
  font-size:clamp(38px, 5vw, 72px);
  line-height:0.98;
  letter-spacing:-0.055em;
  font-weight:950;
  color:var(--text);
  text-shadow:0 10px 30px rgba(0,0,0,0.22);
}

.hero-subtitle{
  position:relative;
  z-index:2;
  margin:0;
  max-width:980px;
  color:var(--muted);
  font-size:16px;
  line-height:1.82;
  text-align:justify;
  text-justify:inter-word;
}

.page-shell{
  position:relative;
  border-radius:34px;
  border:1px solid var(--stroke);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.02)),
    rgba(9,16,32,0.58);
  box-shadow:
    0 30px 90px rgba(0,0,0,0.36),
    inset 0 1px 0 rgba(255,255,255,0.08);
  backdrop-filter: blur(20px) saturate(130%);
  -webkit-backdrop-filter: blur(20px) saturate(130%);
  padding:26px;
  overflow:hidden;
  margin-bottom:18px;
}

.page-shell:before{
  content:"";
  position:absolute;
  inset:-30% auto auto -10%;
  width:360px;
  height:360px;
  border-radius:50%;
  background:radial-gradient(circle, rgba(139,255,230,0.12), transparent 65%);
  filter:blur(16px);
  pointer-events:none;
}

.breadcrumb-line{
  display:flex;
  align-items:center;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}

.breadcrumb-pill{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  border-radius:999px;
  font-size:12px;
  text-transform:uppercase;
  letter-spacing:.12em;
  color:#e8fffa;
  border:1px solid rgba(255,255,255,0.10);
  background:linear-gradient(135deg, rgba(139,255,230,0.08), rgba(138,182,255,0.07));
}

.glass-card{
  position:relative;
  border:1px solid var(--stroke);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03)),
    rgba(255,255,255,0.02);
  border-radius:28px;
  padding:22px;
  backdrop-filter: blur(18px) saturate(130%);
  -webkit-backdrop-filter: blur(18px) saturate(130%);
  box-shadow:
    0 20px 52px rgba(0,0,0,0.26),
    inset 0 1px 0 rgba(255,255,255,0.07);
  margin-top:10px;
  overflow:hidden;
}

.glass-card:before{
  content:"";
  position:absolute;
  inset:-10% auto auto -4%;
  width:180px;
  height:180px;
  border-radius:50%;
  background:radial-gradient(circle, rgba(139,255,230,0.10), transparent 68%);
  filter:blur(14px);
  pointer-events:none;
}

.glass-title{
  position:relative;
  margin:0 0 10px 0;
  font-size:21px;
  font-weight:850;
  color:var(--text);
  letter-spacing:-0.03em;
}

.glass-text{
  position:relative;
  margin:0;
  color:var(--muted);
  line-height:1.78;
  font-size:14px;
}

.timeline{
  display:grid;
  gap:10px;
  margin-top:12px;
}

.timeline-item{
  display:grid;
  grid-template-columns: 36px 1fr;
  gap:14px;
  align-items:flex-start;
  padding:13px 0;
  border-bottom:1px solid rgba(255,255,255,0.08);
}

.timeline-item:last-child{
  border-bottom:none;
}

.timeline-badge{
  width:36px;
  height:36px;
  border-radius:14px;
  background:
    linear-gradient(135deg, rgba(139,255,230,0.98), rgba(138,182,255,0.96));
  color:#08101e;
  display:flex;
  align-items:center;
  justify-content:center;
  font-weight:900;
  box-shadow:
    0 12px 24px rgba(139,255,230,0.20),
    inset 0 1px 0 rgba(255,255,255,0.45);
}

.timeline-item b{
  color:var(--text);
  font-size:14px;
}

.timeline-item div{
  color:var(--muted);
  font-size:13px;
  line-height:1.6;
}

.section-title{
  margin:28px 0 10px 0;
  font-size:31px;
  line-height:1.1;
  letter-spacing:-0.04em;
  color:var(--text);
  font-weight:900;
}

.section-note{
  color:var(--muted);
  margin-top:-2px;
  margin-bottom:20px;
  font-size:16px;
  line-height:1.7;
}

.role-zone{
  position:relative;
  perspective:2200px;
}

.role-stack{
  position:relative;
}

.role-visual-frame{
  position:relative;
  margin-bottom:-8px;
  z-index:1;
  perspective:2200px;
}

.role-visual-card{
  position:relative;
  width:100%;
  height:375px;
  border-radius:42px;
  overflow:hidden;
  transform-style:preserve-3d;
  transform:
    perspective(2200px)
    rotateX(9deg)
    rotateY(-8deg)
    translateY(0)
    translateZ(0);
  box-shadow:
    0 42px 110px rgba(0,0,0,0.34),
    0 14px 34px rgba(0,0,0,0.22),
    inset 0 1px 0 rgba(255,255,255,0.14);
  border:1px solid rgba(255,255,255,0.11);
  background:
    linear-gradient(145deg, rgba(122,232,214,0.20), rgba(138,182,255,0.14) 46%, rgba(255,143,214,0.09) 100%),
    rgba(9,16,32,0.46);
  transition:transform .25s ease, box-shadow .25s ease, filter .25s ease;
}

.role-visual-card:hover{
  transform:
    perspective(2200px)
    rotateX(6deg)
    rotateY(-6deg)
    translateY(-8px)
    scale(1.01);
  box-shadow:
    0 54px 130px rgba(0,0,0,0.38),
    0 0 46px rgba(111,232,255,0.10),
    inset 0 1px 0 rgba(255,255,255,0.15);
  filter:brightness(1.03) saturate(1.02);
}

.role-visual-card img{
  width:100%;
  height:100%;
  object-fit:cover;
  display:block;
  transform:translateZ(18px) scale(1.01);
}

.role-visual-card:before{
  content:"";
  position:absolute;
  inset:0;
  background:
    linear-gradient(180deg, rgba(255,255,255,0.04), transparent 16%, transparent 76%, rgba(0,0,0,0.10)),
    linear-gradient(120deg, rgba(139,255,230,0.10), transparent 28%, transparent 68%, rgba(138,182,255,0.12));
  pointer-events:none;
  z-index:2;
}

.role-visual-card:after{
  content:"";
  position:absolute;
  inset:auto 8% -10% 8%;
  height:40%;
  background:radial-gradient(ellipse at center, rgba(95,173,255,0.18), transparent 68%);
  filter:blur(18px);
  pointer-events:none;
  z-index:0;
}

.role-visual-shine{
  position:absolute;
  inset:-30% auto auto -18%;
  width:62%;
  height:180%;
  background:linear-gradient(90deg, transparent, rgba(255,255,255,0.09), transparent);
  transform:rotate(16deg);
  pointer-events:none;
  z-index:3;
}

.role-visual-fallback{
  display:flex;
  align-items:center;
  justify-content:center;
}

.role-fallback-inner{
  position:relative;
  z-index:4;
  text-align:center;
  padding:24px;
}

.role-fallback-emoji{
  font-size:64px;
  margin-bottom:10px;
}

.role-fallback-title{
  font-size:28px;
  font-weight:900;
  color:var(--text);
  letter-spacing:-0.03em;
  margin-bottom:8px;
}

.role-fallback-note{
  color:var(--muted);
  font-size:14px;
  line-height:1.6;
}

.role-button-wrap{
  position:relative;
  z-index:3;
  margin-top:10px;
}

.role-click{
  position:relative;
  z-index:3;
}

.role-click button{
  position:relative;
  z-index:3;
  min-height:86px !important;
  height:86px !important;
  white-space:normal !important;
  line-height:1.2 !important;
  font-size:24px !important;
  font-weight:900 !important;
  color:#08101e !important;
  text-align:center !important;
  border-radius:24px !important;
  border:1px solid rgba(255,255,255,0.12) !important;
  background:
    linear-gradient(135deg, rgba(139,255,230,1), rgba(111,232,255,1) 42%, rgba(138,182,255,1)) !important;
  box-shadow:
    0 24px 58px rgba(0,0,0,0.30),
    0 18px 34px rgba(111,232,255,0.20),
    inset 0 1px 0 rgba(255,255,255,0.52) !important;
  transform-style:preserve-3d !important;
  transform:
    perspective(1600px)
    rotateX(5deg)
    rotateY(-4deg)
    translateY(0)
    translateZ(0) !important;
  text-shadow:0 1px 0 rgba(255,255,255,0.28);
  transition:
    transform .22s ease,
    filter .22s ease,
    box-shadow .22s ease !important;
}

.role-click button:hover{
  transform:
    perspective(1600px)
    rotateX(4deg)
    rotateY(-3deg)
    translateY(-3px)
    scale(1.008) !important;
  filter:brightness(1.03);
  box-shadow:
    0 30px 70px rgba(0,0,0,0.34),
    0 0 36px rgba(111,232,255,0.14),
    inset 0 1px 0 rgba(255,255,255,0.58) !important;
}

.role-click button:focus{
  outline:none !important;
  box-shadow:
    0 30px 70px rgba(0,0,0,0.34),
    0 0 0 1px rgba(139,255,230,0.30),
    0 0 0 5px rgba(139,255,230,0.08),
    inset 0 1px 0 rgba(255,255,255,0.58) !important;
}

.form-shell{
  border-radius:30px;
  border:1px solid var(--stroke);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03)),
    rgba(8,14,28,0.58);
  backdrop-filter: blur(18px) saturate(130%);
  -webkit-backdrop-filter: blur(18px) saturate(130%);
  box-shadow:
    0 26px 74px rgba(0,0,0,0.32),
    inset 0 1px 0 rgba(255,255,255,0.06);
  padding:24px;
}

.form-topbar{
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:14px;
  flex-wrap:wrap;
  margin-bottom:18px;
}

.form-chip{
  display:inline-flex;
  align-items:center;
  gap:10px;
  padding:10px 14px;
  border-radius:999px;
  background:linear-gradient(135deg, rgba(139,255,230,0.12), rgba(138,182,255,0.10));
  border:1px solid rgba(255,255,255,0.09);
  color:#efffff;
  font-size:13px;
  letter-spacing:.08em;
  text-transform:uppercase;
}

.form-intro{
  color:var(--muted);
  font-size:15px;
  line-height:1.7;
  margin-top:4px;
}

.horse-shell{
  position:relative;
  border-radius:24px;
  border:1px solid rgba(255,255,255,0.08);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.03)),
    rgba(255,255,255,0.02);
  padding:20px 18px 10px 18px;
  margin-bottom:18px;
  overflow:hidden;
  box-shadow:
    0 18px 42px rgba(0,0,0,0.18),
    inset 0 1px 0 rgba(255,255,255,0.05);
}

.horse-shell:before{
  content:"";
  position:absolute;
  inset:-20% auto auto -10%;
  width:220px;
  height:220px;
  border-radius:50%;
  background:radial-gradient(circle, rgba(138,182,255,0.10), transparent 68%);
  filter:blur(18px);
  pointer-events:none;
}

.horse-title{
  position:relative;
  margin:0 0 14px 0;
  font-size:19px;
  font-weight:850;
  letter-spacing:-0.02em;
  color:var(--text);
}

.metric-shell{
  border-radius:22px;
  padding:16px;
  border:1px solid rgba(255,255,255,0.08);
  background:rgba(255,255,255,0.05);
}

div[data-baseweb="input"] > div,
div[data-baseweb="textarea"] > div,
div[data-baseweb="select"] > div,
[data-baseweb="base-input"]{
  border-radius:18px !important;
  background: rgba(255,255,255,0.98) !important;
  border:1px solid rgba(0,0,0,0.04) !important;
  box-shadow:
    0 8px 22px rgba(0,0,0,0.08),
    inset 0 1px 0 rgba(255,255,255,0.90) !important;
}

input, textarea,
div[data-baseweb="select"] span,
div[data-baseweb="select"] div{
  color:#111111 !important;
  -webkit-text-fill-color:#111111 !important;
}

input::placeholder, textarea::placeholder{
  color:#5c6470 !important;
  -webkit-text-fill-color:#5c6470 !important;
  opacity:1 !important;
}

div.stTextInput label, div.stTextArea label, div.stSelectbox label,
div.stNumberInput label, div.stCheckbox label, div.stDateInput label {
  color:var(--text) !important;
  font-weight:600 !important;
}

div.stButton > button,
div.stDownloadButton > button,
button[kind="primary"]{
  width:100%;
  border-radius:18px !important;
  border:1px solid rgba(255,255,255,0.10) !important;
  padding:0.94rem 1rem !important;
  font-weight:850 !important;
  letter-spacing:-0.01em;
  color:#07111f !important;
  background:
    linear-gradient(135deg, rgba(139,255,230,1), rgba(111,232,255,1) 42%, rgba(138,182,255,1)) !important;
  box-shadow:
    0 18px 34px rgba(111,232,255,0.20),
    inset 0 1px 0 rgba(255,255,255,0.52) !important;
}
div.stButton > button:hover,
div.stDownloadButton > button:hover{
  transform: translateY(-1px);
  filter: brightness(1.02);
}

button[data-baseweb="tab"]{
  border-radius:16px !important;
}

hr.soft{
  border:none;
  height:1px;
  background:rgba(255,255,255,0.08);
  margin:18px 0;
}

.admin-shell{
  border-radius:30px;
  padding:22px;
  border:1px solid rgba(255,255,255,0.10);
  background:
    linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03)),
    rgba(8,14,28,0.52);
  box-shadow:
    0 22px 56px rgba(0,0,0,0.24),
    inset 0 1px 0 rgba(255,255,255,0.07);
  backdrop-filter: blur(16px) saturate(125%);
}

.stExpander{
  border-radius:22px !important;
  overflow:hidden !important;
  border:1px solid rgba(255,255,255,0.08) !important;
  background:rgba(255,255,255,0.03) !important;
}

[data-testid="stExpander"] details{
  background:rgba(255,255,255,0.02);
  border-radius:22px !important;
}

.code-shell{
  border-radius:22px;
  border:1px solid rgba(255,255,255,0.09);
  background:linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.02));
  padding:12px;
}

.micro-note{
  color:var(--muted-2);
  font-size:13px;
  line-height:1.7;
}

@media (max-width: 980px){
  .hero-shell{
    padding:30px 22px 24px 22px;
  }
  .page-shell{
    padding:20px;
  }
  .role-visual-card{
    height:320px;
    transform:
      perspective(1800px)
      rotateX(6deg)
      rotateY(-5deg);
  }
  .role-click button{
    min-height:78px !important;
    height:78px !important;
    font-size:22px !important;
  }
}
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ==============================================================================
# HERO
# ==============================================================================
st.markdown(
    """
<div class="hero-shell">
  <div class="kicker">ANSES × RESPE · enquête prospective RESOLVE · Contact (uniquement si nécessaire) : 06.42.13.69.64 || quentin.lamboley@anses.fr</div>
  <div class="hero-title">PROJET RESOLVE<br>Objectif&nbsp;: mieux caractériser la borréliose de Lyme équine en France</div>
  <p class="hero-subtitle">
    Cet site centralise les signalements et les demandes de kits dans le cadre de l’étude RESOLVE financée par l'IFCE et le Fonds Eperon.
    L’objectif est de contribuer au développement et à la validation d’un outil d’aide au diagnostic à partir
    d’une enquête prospective de terrain icluant des chevaux suspectés de Lyme. Nous souhaitons documenter de façon rigoureuse les tableaux cliniques    suspects, homogénéiser les pratiques,
    et disposer de données de terrain solides pour améliorer la classification diagnostique des suspicions de borréliose de Lyme équine.
    Pour chaque cheval inclut dans l'étude, les analyses liées à la borréliose de Lyme sont intégralement prises en charge
    <strong style="color:#ffffff;">(sérologie ELISA + Western Blot + PCR)</strong>, tout comme l’acheminement
    des échantillons vers le laboratoire partenaire qui fera les analyses (LABEO) et la transmission des résultats.
  </p>
</div>
""",
    unsafe_allow_html=True,
)

selected_role = st.session_state.selected_role
current_view = st.session_state.current_view

# ==============================================================================
# HOME
# ==============================================================================
# ==============================================================================
# HOME
# ==============================================================================
if current_view == "home":
    home_left, home_right = st.columns([0.92, 1.08], gap="large")

    with home_left:
        st.markdown(
            """
<div class="glass-card">
  <h3 class="glass-title">PROTOCOLE DE L'ETUDE</h3>
  <div class="timeline">
    <div class="timeline-item">
      <div class="timeline-badge">1</div>
      <div><b>Choix du profil et inscription des chevaux à l'étude.</b></div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">2</div>
      <div><b>Etude de la candidatures, validation des critères d'inclusion et prise de rdv pour les prélèvements</b></div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">3</div>
      <div><b>Réception du kit RESOLVE par le vétérinaire</b></div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">4</div>
      <div><b>Prélèvements sanguins (10 tubes dont 9 secs et 1 EDTA) et éventuels autres prélèvements</b></div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">5</div>
      <div><b>Remplissage du questionnaire et étiquettage</b></div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">6</div>
      <div><b>Envoi d’un message informant du prélèvement au 06.42.13.69.64 pour recevoir le e-bon qui prend en charge l’acheminement des prélèvements.</b>   </div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">7</div>
      <div><b>Construction du colis et envoi à :</b><br><br>Laboratoire LABEO (Frank Duncombe)<br>1 route Rosel<br>14 280 Saint Contest</div>
    </div>
    <div class="timeline-item">
      <div class="timeline-badge">8</div>
      <div><b>Transmission des résultats et amélioration de l’outil diagnostique RESOLVE</b></div>
    </div>
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

    with home_right:
        st.markdown(
            '<div class="section-title" style="font-size:24px; line-height:1.2; margin-top:10px; white-space:nowrap;">Vous souhaitez participer ? Choisissez votre profil</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="section-note">Sélectionnez le parcours qui correspond à votre situation, puis poursuivez vers une page dédiée au formulaire d’inclusion.</div>',
            unsafe_allow_html=True,
        )

        role_cols = st.columns(2, gap="large")
        for col, key in zip(role_cols, ["detenteur", "veterinaire"]):
            cfg = ROLE_OPTIONS[key]
            with col:
                st.markdown('<div class="role-zone"><div class="role-stack">', unsafe_allow_html=True)
                render_role_visual(
                    image_path=cfg["image_path"],
                    fallback_emoji=cfg["emoji"],
                    fallback_title=cfg["label"],
                )
                st.markdown('<div class="role-button-wrap"><div class="role-click">', unsafe_allow_html=True)
                if st.button(
                    cfg["label"],
                    key=f"select_role_{key}",
                    use_container_width=True,
                ):
                    go_form(key)
                    st.rerun()
                st.markdown("</div></div></div></div>", unsafe_allow_html=True)

# ==============================================================================
# FORM PAGE
# ==============================================================================
if current_view == "form" and selected_role:
    st.markdown('<div class="page-shell">', unsafe_allow_html=True)

    with st.container():
        top_row_left, top_row_right = st.columns([0.18, 0.82], gap="medium")

        with top_row_left:
            st.markdown(
                """
<style>
div.stButton > button[kind="secondary"],
div.stButton > button[kind="secondaryFormSubmit"]{
  white-space: nowrap !important;
}
button[kind="secondary"][data-testid="baseButton-secondary"]{
  min-height: 44px !important;
  height: 44px !important;
  padding: 0.35rem 1rem !important;
  font-size: 13px !important;
  border-radius: 999px !important;
  line-height: 1 !important;
  margin-top: 0 !important;
}
</style>
""",
                unsafe_allow_html=True,
            )
            if st.button("← Retour au profil", key="back_to_home_top_left", use_container_width=True):
                go_home()
                st.rerun()

        with top_row_right:
            st.markdown(
                f"""
<div class="breadcrumb-line" style="margin-bottom:8px; align-items:center;">
  <div class="breadcrumb-pill">RESOLVE</div>
  <div class="breadcrumb-pill">{ROLE_OPTIONS[selected_role]["emoji"]} {ROLE_OPTIONS[selected_role]["label"]}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown(
            f"""
<div class="section-title" style="margin-top:0; margin-left:0;">Formulaire RESOLVE</div>
<div class="section-note" style="margin-left:0;">
  Vous êtes sur une page dédiée au parcours"<strong>{ROLE_OPTIONS[selected_role]["label"]}</strong>.
  "Complétez les informations ci-dessous pour enregistrer votre demande proprement et de manière structurée.
</div>
""",
            unsafe_allow_html=True,
        )

    with st.container():
        st.markdown('<div class="form-shell">', unsafe_allow_html=True)

        st.markdown(
            """
<div class="form-intro">
  Renseignez les coordonnées nécessaires, puis ajoutez un ou plusieurs chevaux.</div>
""",
            unsafe_allow_html=True,
        )

        st.number_input(
            "Nombre de chevaux à renseigner",
            min_value=1,
            max_value=20,
            step=1,
            key="horse_count",
            help="Ajoutez autant de chevaux que nécessaire. Un bloc complet sera généré pour chacun.",
        )

        with st.expander("👤 Vos coordonnées", expanded=False):
            c1, c2 = st.columns(2, gap="medium")
            with c1:
                contact_prenom = st.text_input("Prénom *", placeholder="Ex : Marie", key=f"contact_prenom_{selected_role}")
            with c2:
                contact_nom = st.text_input("Nom *", placeholder="Ex : Dupont", key=f"contact_nom_{selected_role}")

            c3, c4 = st.columns(2, gap="medium")
            with c3:
                contact_email = st.text_input("Mail *", placeholder="Ex : marie.dupont@gmail.com", key=f"contact_email_{selected_role}")
            with c4:
                contact_telephone = st.text_input("Téléphone *", placeholder="Ex : 06 12 34 56 78", key=f"contact_telephone_{selected_role}")

            c6, c7, c8 = st.columns(3, gap="medium")
            with c6:
                contact_ville = st.text_input("Ville", placeholder="Ex : Rambouillet", key=f"contact_ville_{selected_role}")
            with c7:
                contact_code_postal = st.text_input("Code postal", placeholder="Ex : 78120", key=f"contact_code_postal_{selected_role}")
            with c8:
                contact_region = st.text_input("Région", placeholder="Ex : Île-de-France", key=f"contact_region_{selected_role}")

            contact_adresse = st.text_area(
                "Adresse complète",
                placeholder="N° et rue, ville, code postal",
                height=90,
                key=f"contact_adresse_{selected_role}",
            )

            if selected_role == "veterinaire":
                st.markdown("**Coordonnées professionnelles vétérinaires**")
                p1, p2 = st.columns(2, gap="medium")
                with p1:
                    contact_structure = st.text_input(
                        "Nom de la structure vétérinaire *",
                        placeholder="Ex : Clinique Vétérinaire des Yvelines",
                        key=f"contact_structure_{selected_role}",
                    )
                with p2:
                    st.text_input(
                        "Information",
                        value="La région de la clinique n'a pas besoin d'être celle du cheval.",
                        disabled=True,
                        key=f"info_structure_{selected_role}",
                    )
            else:
                contact_structure = ""
        if selected_role == "detenteur":
            with st.expander("🩺 Coordonnées du vétérinaire à contacter", expanded=False):
                v1, v2 = st.columns(2, gap="medium")
                with v1:
                    veterinaire_prenom = st.text_input("Prénom du vétérinaire *", placeholder="Ex : Camille", key="veterinaire_prenom_detenteur")
                with v2:
                    veterinaire_nom = st.text_input("Nom du vétérinaire *", placeholder="Ex : Martin", key="veterinaire_nom_detenteur")

                v3, v4 = st.columns(2, gap="medium")
                with v3:
                    veterinaire_email = st.text_input("Mail du vétérinaire *", placeholder="Ex : clinique@veto.fr", key="veterinaire_email_detenteur")
                with v4:
                    veterinaire_telephone = st.text_input("Téléphone du vétérinaire *", placeholder="Ex : 06 00 00 00 00", key="veterinaire_telephone_detenteur")

                v5, v6, v7, v8 = st.columns(4, gap="medium")
                with v5:
                    veterinaire_structure = st.text_input("Structure vétérinaire *", placeholder="Ex : Clinique équine du Val", key="veterinaire_structure_detenteur")
                with v6:
                    veterinaire_ville = st.text_input("Ville de la structure", placeholder="Ex : Caen", key="veterinaire_ville_detenteur")
                with v7:
                    veterinaire_code_postal = st.text_input("Code postal de la structure", placeholder="Ex : 14000", key="veterinaire_code_postal_detenteur")
                with v8:
                    veterinaire_region = st.text_input("Région de la structure", placeholder="Ex : Normandie", key="veterinaire_region_detenteur")

                veterinaire_adresse = st.text_area(
                    "Adresse de la structure vétérinaire",
                    placeholder="Adresse complète si disponible",
                    height=90,
                    key="veterinaire_adresse_detenteur",
                )
        else:
            veterinaire_prenom = ""
            veterinaire_nom = ""
            veterinaire_email = ""
            veterinaire_telephone = ""
            veterinaire_structure = ""
            veterinaire_ville = ""
            veterinaire_code_postal = ""
            veterinaire_region = ""
            veterinaire_adresse = ""

        horses = []
        for i in range(int(st.session_state.horse_count)):
            horse_index = i + 1
            with st.expander(f"🐎 Cheval {horse_index}", expanded=False):
                st.markdown('<div class="horse-shell">', unsafe_allow_html=True)
                st.markdown(f'<div class="horse-title">🐎 Cheval {horse_index}</div>', unsafe_allow_html=True)

                h1, h2, h3, h4 = st.columns(4, gap="medium")
                with h1:
                    cheval_nom = st.text_input(
                        "Nom du cheval *",
                        placeholder="Ex : Quartz",
                        key=f"cheval_nom_{selected_role}_{i}",
                    )
                with h2:
                    cheval_age = st.text_input(
                        "Âge",
                        placeholder="Ex : 12",
                        key=f"cheval_age_{selected_role}_{i}",
                    )
                with h3:
                    cheval_sexe = st.selectbox(
                        "Sexe",
                        ["", "Jument", "Hongre", "Entier", "Inconnu"],
                        index=0,
                        key=f"cheval_sexe_{selected_role}_{i}",
                    )
                with h4:
                    cheval_race = st.text_input(
                        "Race",
                        placeholder="Ex : Selle Français",
                        key=f"cheval_race_{selected_role}_{i}",
                    )

                h5, h6 = st.columns(2, gap="medium")
                with h5:
                    cheval_commune = st.text_input(
                        "Commune de détention",
                        placeholder="Ex : Lisieux",
                        key=f"cheval_commune_{selected_role}_{i}",
                    )
                with h6:
                    cheval_departement = st.text_input(
                        "Département",
                        placeholder="Ex : Calvados",
                        key=f"cheval_departement_{selected_role}_{i}",
                    )

                cheval_lieu_detention_coordonnees = st.text_area(
                    "Lieu de détention du cheval *",
                    placeholder="Adresse complète, indications utiles, coordonnées GPS si disponibles…",
                    height=110,
                    key=f"cheval_lieu_detention_coordonnees_{selected_role}_{i}",
                )

                st.markdown("**Critères d’inclusion**")
                ci1, ci2, ci3 = st.columns(3, gap="medium")
                with ci1:
                    contact_regulier_tiques_vegetation = st.checkbox(
                        "Contact régulier avec tiques / végétation *",
                        value=False,
                        key=f"contact_regulier_tiques_vegetation_{selected_role}_{i}",
                    )
                with ci2:
                    aucune_maladie_precise_connue = st.checkbox(
                        "Aucune maladie précise connue à ce jour *",
                        value=False,
                        key=f"aucune_maladie_precise_connue_{selected_role}_{i}",
                    )
                with ci3:
                    signes_cliniques_evocateurs = st.checkbox(
                        "Présence de signes cliniques évocateurs *",
                        value=False,
                        key=f"signes_cliniques_evocateurs_{selected_role}_{i}",
                    )

                signes_cliniques_generaux = False
                signes_cliniques_articulaires = False
                signes_cliniques_oculaires = False
                signes_cliniques_cutanes = False
                accord_prelevement_liquide_synovial = False
                accord_prelevement_humeur_aqueuse = False
                accord_prelevement_cutane = False

                if signes_cliniques_evocateurs:
                    st.markdown("**Types de signes cliniques**")
                    sc1, sc2, sc3, sc4 = st.columns(4, gap="medium")
                    with sc1:
                        signes_cliniques_generaux = st.checkbox(
                            "Signes cliniques généraux",
                            value=False,
                            key=f"signes_cliniques_generaux_{selected_role}_{i}",
                        )
                    with sc2:
                        signes_cliniques_articulaires = st.checkbox(
                            "Signes cliniques articulaires",
                            value=False,
                            key=f"signes_cliniques_articulaires_{selected_role}_{i}",
                        )
                    with sc3:
                        signes_cliniques_oculaires = st.checkbox(
                            "Signes cliniques oculaires",
                            value=False,
                            key=f"signes_cliniques_oculaires_{selected_role}_{i}",
                        )
                    with sc4:
                        signes_cliniques_cutanes = st.checkbox(
                            "Signes cliniques cutanés",
                            value=False,
                            key=f"signes_cliniques_cutanes_{selected_role}_{i}",
                        )

                    if selected_role == "detenteur":
                        if signes_cliniques_articulaires:
                            accord_prelevement_liquide_synovial = st.checkbox(
                                "J’accepte que mon vétérinaire réalise un prélèvement de liquide synovial. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la fiabilité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_liquide_synovial_{selected_role}_{i}",
                            )
                        if signes_cliniques_oculaires:
                            accord_prelevement_humeur_aqueuse = st.checkbox(
                                "J’accepte que mon vétérinaire réalise un prélèvement d’humeur aqueuse. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la capacité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_humeur_aqueuse_{selected_role}_{i}",
                            )
                        if signes_cliniques_cutanes:
                            accord_prelevement_cutane = st.checkbox(
                                "J’accepte que mon vétérinaire réalise un prélèvement cutané. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la capacité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_cutane_{selected_role}_{i}",
                            )
                    else:
                        if signes_cliniques_articulaires:
                            accord_prelevement_liquide_synovial = st.checkbox(
                                "Les propriétaires sont d’accord pour réaliser un prélèvement de liquide synovial. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la capacité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_liquide_synovial_{selected_role}_{i}",
                            )
                        if signes_cliniques_oculaires:
                            accord_prelevement_humeur_aqueuse = st.checkbox(
                                "Les propriétaires sont d’accord pour réaliser un prélèvement d’humeur aqueuse. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la capacité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_humeur_aqueuse_{selected_role}_{i}",
                            )
                        if signes_cliniques_cutanes:
                            accord_prelevement_cutane = st.checkbox(
                                "Les propriétaires sont d’accord pour réaliser un prélèvement cutané. La PCR sera alors réalisée sur ce prélèvement, accroissant considérablement la capacité du diagnostic.",
                                value=False,
                                key=f"accord_prelevement_cutane_{selected_role}_{i}",
                            )

                resume_signes_cliniques = st.text_area(
                    "Quels signes cliniques observez-vous ? *",
                    placeholder="Décrivez les signes cliniques évocateurs observés.",
                    height=140,
                    key=f"resume_signes_cliniques_{selected_role}_{i}",
                )

                accord_bilan_sanguin_complet = False
                accord_test_negatif_piroplasmose = False
                accord_test_negatif_ehrlichiose = False

                if normalize_spaces(resume_signes_cliniques):
                    question_label = (
                        "Compte tenu de la non spécificité des signes cliniques, seriez-vous d'accord pour que votre vétérinaire réalise :"
                        if selected_role == "detenteur"
                        else "Compte tenu de la non spécificité des signes cliniques, est-ce que les propriétaires seraient d'accord pour que le vétérinaire réalise :"
                    )
                    st.markdown(f"**{question_label}**")
                    accord_bilan_sanguin_complet = st.checkbox(
                        "Bilan sanguin complet avec NFS, paramètres musculaires, rénaux, hépatiques, fibrinogène et/ou SAA",
                        value=False,
                        key=f"accord_bilan_sanguin_complet_{selected_role}_{i}",
                    )
                    accord_test_negatif_piroplasmose = st.checkbox(
                        "test piroplasmose",
                        value=False,
                        key=f"accord_test_negatif_piroplasmose_{selected_role}_{i}",
                    )
                    accord_test_negatif_ehrlichiose = st.checkbox(
                        "test ehrlichiose",
                        value=False,
                        key=f"accord_test_negatif_ehrlichiose_{selected_role}_{i}",
                    )

                contexte_large = st.text_area(
                    "Contexte au sens large *",
                    placeholder="Historique, évolution, examens déjà réalisés, exposition environnementale, contexte de suspicion, remarques utiles…",
                    height=180,
                    key=f"contexte_large_{selected_role}_{i}",
                )

                st.markdown("</div>", unsafe_allow_html=True)

                horses.append(
                    {
                        "cheval_nom": cheval_nom,
                        "cheval_age": cheval_age,
                        "cheval_sexe": cheval_sexe,
                        "cheval_race": cheval_race,
                        "cheval_commune": cheval_commune,
                        "cheval_departement": cheval_departement,
                        "cheval_lieu_detention_coordonnees": cheval_lieu_detention_coordonnees,
                        "contact_regulier_tiques_vegetation": contact_regulier_tiques_vegetation,
                        "aucune_maladie_precise_connue": aucune_maladie_precise_connue,
                        "signes_cliniques_evocateurs": signes_cliniques_evocateurs,
                        "signes_cliniques_generaux": signes_cliniques_generaux,
                        "signes_cliniques_articulaires": signes_cliniques_articulaires,
                        "signes_cliniques_oculaires": signes_cliniques_oculaires,
                        "signes_cliniques_cutanes": signes_cliniques_cutanes,
                        "accord_prelevement_liquide_synovial": accord_prelevement_liquide_synovial,
                        "accord_prelevement_humeur_aqueuse": accord_prelevement_humeur_aqueuse,
                        "accord_prelevement_cutane": accord_prelevement_cutane,
                        "resume_signes_cliniques": resume_signes_cliniques,
                        "accord_bilan_sanguin_complet": accord_bilan_sanguin_complet,
                        "accord_test_negatif_piroplasmose": accord_test_negatif_piroplasmose,
                        "accord_test_negatif_ehrlichiose": accord_test_negatif_ehrlichiose,
                        "contexte_large": contexte_large,
                    }
                )

        with st.expander("📦 Logistique & consentements", expanded=False):
            if selected_role == "veterinaire":
                procedure_diagnostique_habituelle = st.text_area(
                    "En tant que vétérinaire de terrain, quelle est votre procédure diagnostique habituelle pour Lyme ?",
                    placeholder="Réponse libre visible dans l’espace administrateur.",
                    height=130,
                    key=f"procedure_diagnostique_habituelle_{selected_role}",
                )
            else:
                procedure_diagnostique_habituelle = ""

            l1, l2 = st.columns(2, gap="medium")
            with l1:
                a_besoin_kit_resolve = st.checkbox(
                    "Je souhaite recevoir / faire envoyer un kit RESOLVE",
                    value=True,
                    key=f"a_besoin_kit_resolve_{selected_role}",
                )
                souhaite_etre_recontacte = st.checkbox(
                    "J’accepte d’être recontacté au sujet de cette inclusion",
                    value=True,
                    key=f"souhaite_etre_recontacte_{selected_role}",
                )
            with l2:
                consentement_contact = st.checkbox(
                    "J’autorise l’équipe RESOLVE à utiliser mes coordonnées pour me contacter dans le cadre de l’étude *",
                    value=False,
                    key=f"consentement_contact_{selected_role}",
                )
                consentement_donnees = st.checkbox(
                    "J’accepte la transmission et le traitement des informations fournies dans le cadre de l’étude RESOLVE *",
                    value=False,
                    key=f"consentement_donnees_{selected_role}",
                )

        submit = st.button("✨ Envoyer ma demande RESOLVE", key=f"submit_resolve_{selected_role}", use_container_width=True)

        st.markdown("</div>", unsafe_allow_html=True)

    if submit:
        payload = {
            "profil": selected_role,
            "contact_prenom": contact_prenom,
            "contact_nom": contact_nom,
            "contact_email": contact_email,
            "contact_telephone": contact_telephone,
            "contact_structure": contact_structure,
            "contact_adresse": contact_adresse,
            "contact_ville": contact_ville,
            "contact_code_postal": contact_code_postal,
            "contact_region": contact_region,
            "veterinaire_prenom": veterinaire_prenom,
            "veterinaire_nom": veterinaire_nom,
            "veterinaire_email": veterinaire_email,
            "veterinaire_telephone": veterinaire_telephone,
            "veterinaire_structure": veterinaire_structure,
            "veterinaire_adresse": veterinaire_adresse,
            "veterinaire_ville": veterinaire_ville,
            "veterinaire_code_postal": veterinaire_code_postal,
            "veterinaire_region": veterinaire_region,
            "horses": horses,
            "souhaite_etre_recontacte": souhaite_etre_recontacte,
            "consentement_contact": consentement_contact,
            "consentement_donnees": consentement_donnees,
            "a_besoin_kit_resolve": a_besoin_kit_resolve,
            "procedure_diagnostique_habituelle": procedure_diagnostique_habituelle,
        }

        errors = validate_submission(payload)
        if errors:
            st.error("Merci de corriger les éléments suivants :")
            for e in errors:
                st.write(f"- {e}")
        else:
            try:
                rows = build_submission_rows(payload)
                append_submission(rows)

                pdf_bytes = create_submission_pdf_bytes(payload, rows)
                st.session_state.last_pdf_bytes = pdf_bytes
                st.session_state.last_pdf_filename = make_pdf_filename(selected_role)

                try:
                    send_pdf_email(
                        pdf_bytes=pdf_bytes,
                        pdf_filename=st.session_state.last_pdf_filename,
                        to_email=st.secrets["ADMIN_EMAIL"],
                        subject="Nouvelle demande RESOLVE",
                        body=(
                            "Bonjour,\n\n"
                            "Une nouvelle demande RESOLVE a été enregistrée.\n"
                            "Le PDF récapitulatif est en pièce jointe.\n\n"
                            "Cordialement,"
                        ),
                    )
                except Exception as mail_error:
                    st.warning(
                        f"La demande a bien été enregistrée, mais l’envoi automatique du PDF a échoué : {mail_error}"
                    )

                st.success("✅ Votre demande a bien été enregistrée.")
                if selected_role == "detenteur":
                    st.info(
                        "Merci pour votre signalement. Les coordonnées du vétérinaire ont bien été enregistrées afin qu’il puisse être contacté et recevoir les éléments nécessaires."
                    )
                else:
                    st.info(
                        "Merci pour votre participation à l’étude RESOLVE. Votre demande de kit et les informations d’inclusion ont bien été prises en compte."
                    )

                st.markdown(
                    """
<div class="glass-card" style="margin-top:14px;">
  <h3 class="glass-title">Rappel logistique</h3>
  <p class="glass-text">
    Les analyses de borréliose de Lyme équine (ELISA + WB + PCR) sont prises en charge,
    tout comme l’acheminement des échantillons. Après le prélèvement, un message doit être envoyé
    au 06 42 13 69 64 pour recevoir le e-bon de transport, puis le colis doit être construit avec
    l’ensemble des pièces nécessaires et adressé au Laboratoire LABEO (Frank Duncombe),
    1 route Rosel, 14 280 Saint Contest.
  </p>
</div>
""",
                    unsafe_allow_html=True,
                )
                st.markdown('<div class="code-shell">', unsafe_allow_html=True)
                st.code("\n".join([row["submission_id"] for row in rows]), language=None)
                st.markdown("</div>", unsafe_allow_html=True)

                if st.session_state.last_pdf_bytes and st.session_state.last_pdf_filename:
                    st.download_button(
                        label="⬇️ Télécharger le PDF récapitulatif",
                        data=st.session_state.last_pdf_bytes,
                        file_name=st.session_state.last_pdf_filename,
                        mime="application/pdf",
                        key=f"download_pdf_{selected_role}",
                    )
            except Exception as e:
                st.error("Une erreur est survenue lors de l’enregistrement. Merci de réessayer.")
                st.caption(f"Détail technique (admin) : {e}")

    st.markdown("</div>", unsafe_allow_html=True)

# ==============================================================================
# ADMIN
# ==============================================================================
st.write("")
with st.expander("🔒 Espace administrateur"):
    st.markdown('<div class="admin-shell">', unsafe_allow_html=True)
    admin_pass = st.text_input(
        "Mot de passe administrateur",
        type="password",
        placeholder="Entrer le mot de passe admin",
        key="admin_password_input",
    )

    if admin_pass:
        if admin_pass == ADMIN_PASSWORD:
            submissions_df, samples_df = load_all_data()
            st.success("Accès administrateur autorisé ✅")

            m1, m2, m3, m4 = st.columns(4, gap="medium")
            with m1:
                st.metric("Demandes totales", len(submissions_df))
            with m2:
                st.metric(
                    "Demandes détenteurs",
                    int((submissions_df["profil"] == "detenteur").sum()) if not submissions_df.empty else 0,
                )
            with m3:
                st.metric(
                    "Demandes vétérinaires",
                    int((submissions_df["profil"] == "veterinaire").sum()) if not submissions_df.empty else 0,
                )
            with m4:
                st.metric("Prélèvements géolocalisés", len(filter_map_df(samples_df)))

            admin_tabs = st.tabs(
                [
                    "Inscriptions",
                    "Carte des prélèvements",
                    "Ajouter un prélèvement",
                    "Téléchargement",
                ]
            )

            with admin_tabs[0]:
                st.markdown("#### Inscriptions enregistrées")
                if submissions_df.empty:
                    st.info("Aucune inscription enregistrée pour le moment.")
                else:
                    st.dataframe(submissions_df, use_container_width=True, height=420)

            with admin_tabs[1]:
                st.markdown("#### Carte des prélèvements")
                map_df = filter_map_df(samples_df)
                if map_df.empty:
                    st.info("Aucun prélèvement géolocalisé n’est encore enregistré.")
                else:
                    st.map(map_df, latitude="lat", longitude="lon", size=14, zoom=5, use_container_width=True)
                    st.dataframe(samples_df, use_container_width=True, height=260)

            with admin_tabs[2]:
                st.markdown("#### Ajouter un lieu de prélèvement")
                with st.form("sample_location_form", clear_on_submit=True):
                    s1, s2 = st.columns(2, gap="medium")
                    with s1:
                        submission_options = [""]
                        if not submissions_df.empty and "submission_id" in submissions_df.columns:
                            submission_options += [str(x) for x in submissions_df["submission_id"].dropna().astype(str).tolist()]
                        selected_submission_id = st.selectbox(
                            "ID de dossier associé",
                            options=submission_options,
                            index=0,
                            help="Optionnel mais recommandé pour rattacher le prélèvement à un dossier.",
                        )
                        sample_cheval_nom = st.text_input("Nom du cheval", placeholder="Ex : Quartz")
                        sample_type = st.selectbox(
                            "Type de prélèvement",
                            ["Sang", "Articulaire", "Oculaire", "Autre"],
                            index=0,
                        )
                        sample_date = st.date_input("Date du prélèvement")
                        sample_status = st.selectbox(
                            "Statut du prélèvement",
                            ["réalisé", "envoyé", "reçu laboratoire", "analysé", "autre"],
                            index=0,
                        )

                    with s2:
                        sample_address = st.text_area(
                            "Adresse du lieu de prélèvement",
                            placeholder="Adresse complète ou indication du site",
                            height=100,
                        )
                        s_city, s_cp, s_region = st.columns(3, gap="small")
                        with s_city:
                            sample_city = st.text_input("Ville")
                        with s_cp:
                            sample_cp = st.text_input("Code postal")
                        with s_region:
                            sample_region = st.text_input("Région")

                    g1, g2 = st.columns(2, gap="medium")
                    with g1:
                        sample_lat = st.text_input("Latitude *", placeholder="Ex : 48.8566")
                    with g2:
                        sample_lon = st.text_input("Longitude *", placeholder="Ex : 2.3522")

                    sample_comment = st.text_area(
                        "Commentaire admin",
                        placeholder="Précisions complémentaires sur le prélèvement, la tournée, le colis, etc.",
                        height=120,
                    )

                    sample_submit = st.form_submit_button("➕ Enregistrer le prélèvement")

                if sample_submit:
                    lat = safe_float(sample_lat)
                    lon = safe_float(sample_lon)

                    sample_errors = []
                    if not normalize_spaces(sample_cheval_nom):
                        sample_errors.append("Le nom du cheval est obligatoire.")
                    if lat is None or lon is None:
                        sample_errors.append("Latitude et longitude obligatoires.")
                    elif not is_valid_lat_lon(lat, lon):
                        sample_errors.append("Latitude / longitude invalides.")

                    if sample_errors:
                        st.error("Merci de corriger les éléments suivants :")
                        for e in sample_errors:
                            st.write(f"- {e}")
                    else:
                        sample_row = {
                            "sample_id": make_sample_id(),
                            "timestamp_utc": utc_now_iso(),
                            "submission_id": normalize_spaces(selected_submission_id),
                            "cheval_nom": normalize_spaces(sample_cheval_nom),
                            "type_prelevement": normalize_spaces(sample_type),
                            "date_prelevement": str(sample_date),
                            "statut_prelevement": normalize_spaces(sample_status),
                            "adresse_site": normalize_spaces(sample_address),
                            "ville_site": normalize_spaces(sample_city),
                            "code_postal_site": normalize_spaces(sample_cp),
                            "region_site": normalize_spaces(sample_region),
                            "latitude": lat,
                            "longitude": lon,
                            "commentaire": normalize_spaces(sample_comment),
                        }
                        try:
                            append_sample_location(sample_row)
                            st.success("Prélèvement enregistré avec succès.")
                            st.rerun()
                        except Exception as e:
                            st.error("Erreur lors de l’enregistrement du prélèvement.")
                            st.caption(f"Détail technique : {e}")

            with admin_tabs[3]:
                st.markdown("#### Export des données")
                if os.path.exists(XLSX_PATH):
                    st.download_button(
                        label="⬇️ Télécharger le classeur Excel complet",
                        data=get_download_bytes(XLSX_PATH),
                        file_name="resolve_study_data.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                else:
                    st.info("Aucun fichier Excel disponible pour le moment.")

                if not submissions_df.empty:
                    st.download_button(
                        label="⬇️ Télécharger les inscriptions (CSV)",
                        data=submissions_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="resolve_submissions.csv",
                        mime="text/csv",
                    )

                if not samples_df.empty:
                    st.download_button(
                        label="⬇️ Télécharger les prélèvements (CSV)",
                        data=samples_df.to_csv(index=False).encode("utf-8-sig"),
                        file_name="resolve_sample_locations.csv",
                        mime="text/csv",
                    )

            st.caption("Les données sont stockées côté serveur et centralisées dans un fichier Excel multi-feuilles.")
        else:
            st.error("Mot de passe incorrect.")
    st.markdown("</div>", unsafe_allow_html=True)
