import streamlit as st
import pandas as pd
import uuid
import pulp
import altair as alt
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo # NOUVEAU : Pour forcer le fuseau horaire français
import colorsys
import gspread
from google.oauth2.service_account import Credentials
import itertools
import time

import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="Planning Optimisé & Multisites", layout="wide")

PLANNINGS_DISPOS = ["Planning 250", "Planning 100", "Planning 50"]

if 'assignations_forcees' not in st.session_state:
    st.session_state.assignations_forcees = []

# ==========================================
# GESTION DU TEMPS ET DE LA DEADLINE
# ==========================================
def get_planning_context():
    # On force l'heure de Paris pour éviter les bugs de serveur hébergé à l'étranger
    tz = ZoneInfo("Europe/Paris")
    now = datetime.now(tz)
    
    current_weekday = now.weekday() # 0 = Lundi, 5 = Samedi
    
    # Calcul du samedi de la semaine en cours à 12h00
    days_to_saturday = 5 - current_weekday
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    saturday_noon = today_start + timedelta(days=days_to_saturday, hours=12)
    
    if now < saturday_noon:
        is_locked = False
        target_date = now + timedelta(days=7) # On prépare la semaine prochaine
    else:
        is_locked = True
        target_date = saturday_noon + timedelta(days=7) # Le calcul cible toujours la semaine prochaine
        
    target_year, target_week, _ = target_date.isocalendar()
    return target_year, target_week, saturday_noon, is_locked, now

target_year, target_week, deadline_dt, is_locked, now_local = get_planning_context()

# ==========================================
# INITIALISATION DE FIREBASE
# ==========================================
if not firebase_admin._apps:
    cred = credentials.Certificate(dict(st.secrets["firebase"]))
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# GESTION DES DONNÉES (CLOUD FIRESTORE)
# ==========================================
@st.cache_data(ttl=60)
def charger_donnees():
    docs = db.collection('dispos').stream()
    return [doc.to_dict() for doc in docs]

def ajouter_dispo(dispo_data):
    db.collection('dispos').document(dispo_data["id"]).set(dispo_data)
    charger_donnees.clear()

def supprimer_entree(id_entree):
    db.collection('dispos').document(id_entree).delete()
    charger_donnees.clear()

def vider_toutes_les_dispos():
    docs = db.collection('dispos').stream()
    batch = db.batch()
    count = 0
    for doc in docs:
        batch.delete(doc.reference)
        count += 1
        if count % 500 == 0:
            batch.commit()
            batch = db.batch()
    if count % 500 != 0:
        batch.commit()
    charger_donnees.clear()

@st.cache_data(ttl=300)
def charger_matrice_affinites():
    doc = db.collection('config').document('affinites').get()
    if doc.exists:
        return doc.to_dict()
    return {
        "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
        "100": {"Planning 250": 1, "Planning 100": 3, "Planning 50": 3},
        "50": {"Planning 250": 1, "Planning 100": 1, "Planning 50": 3},
        "pause_interdite_min": 6,
        "repos_nuit_min": 10
    }

def sauvegarder_matrice_affinites(data):
    db.collection('config').document('affinites').set(data)
    charger_matrice_affinites.clear()

def publier_planning_officiel(planning_data, resolution, visible=True, annee=target_year, semaine=target_week):
    # On sauvegarde le planning officiel avec une clé unique pour l'année et la semaine
    doc_id = f"officiel_{annee}_S{semaine}"
    db.collection('plannings').document(doc_id).set({
        "donnees": planning_data,
        "resolution": resolution,
        "visible": visible,
        "annee": annee,
        "semaine": semaine
    })

def charger_planning_officiel(annee=target_year, semaine=target_week):
    doc_id = f"officiel_{annee}_S{semaine}"
    doc = db.collection('plannings').document(doc_id).get()
    if doc.exists:
        data = doc.to_dict()
        return data.get("donnees", []), data.get("resolution", 30), data.get("visible", True)
    return [], 30, False

# ==========================================
# CONNEXION GOOGLE SHEETS
# ==========================================
@st.cache_resource
def get_gspread_client():
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(dict(st.secrets["google_sheets"]), scopes=scopes)
    return gspread.authorize(creds)

def mettre_a_jour_google_sheet(planning, resolution):
    if not planning: return
    client = get_gspread_client()
    sheet_id = "1wl_RLPs1h7TsUQFDQj6An0ouhU1-KlXEmBURksGDPic" 
    sheet = client.open_by_key(sheet_id).sheet1

    df = pd.DataFrame(planning)
    if not df.empty:
        df["Joueurs_Liste"] = df["Joueurs_Liste"].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)
        df["En_Tete"] = df["Jour"] + " | " + df["Planning"].str.replace("Planning ", "")
        df_pivot = df.pivot(index="Horaire", columns="En_Tete", values="Joueurs_Liste").fillna("")
        jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        plannings = ["250", "100", "50"]
        colonnes_triees = [f"{j} | {p}" for j in jours for p in plannings if f"{j} | {p}" in df_pivot.columns]
        df_pivot = df_pivot[colonnes_triees]
        df_export = df_pivot.reset_index()

        def formater_horaire(h_str):
            h_obj = datetime.strptime(h_str, '%H:%M')
            h_fin = (h_obj + timedelta(minutes=resolution)).strftime('%H:%M')
            return f"{h_str} - {h_fin}"
            
        df_export["Horaire"] = df_export["Horaire"].apply(formater_horaire)
        sheet.clear()
        sheet.update([df_export.columns.values.tolist()] + df_export.values.tolist())
        sheet.freeze(rows=1, cols=1)
        sheet.format("A1:Z1", {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9}})

# --- MOTEUR D'OPTIMISATION MATHÉMATIQUE (PuLP) ---
def optimiser_planning_hebdo(donnees_totales, resolution, assignations_forcees, matrice_affinites, gap_rel=0.005, time_limit=600):
    if not donnees_totales: return [], {}, 'No Data'
    dict_joueurs = {d["nom"]: d for d in donnees_totales}
    dict_dispos_jour = {}
    for d in donnees_totales:
        cle = f"{d['nom']}_{d['jour']}"
        dict_dispos_jour.setdefault(cle, []).append(d)

    prob = pulp.LpProblem("Optimisation_Hebdomadaire", pulp.LpMaximize)
    jours_semaine = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    creneaux_globaux = []
    
    for jour in jours_semaine:
        actuel = datetime.strptime("00:00", "%H:%M")
        fin = datetime.strptime("23:59", "%H:%M")
        while actuel < fin:
            creneaux_globaux.append(f"{jour}_{actuel.strftime('%H:%M')}")
            actuel += timedelta(minutes=resolution)

    joueurs = list(dict_joueurs.keys())
    
    X = pulp.LpVariable.dicts("Assign", ((j, p, c) for j in joueurs for p in PLANNINGS_DISPOS for c in creneaux_globaux), cat='Binary')
    Y = pulp.LpVariable.dicts("Joue", ((j, c) for j in joueurs for c in creneaux_globaux), cat='Binary')

    Temps_Total = pulp.LpVariable.dicts("TempsTotalHebdo", joueurs, lowBound=0, cat='Integer')
    Max_Temps = pulp.LpVariable("MaxTempsHebdo", lowBound=0, cat='Integer')
    Min_Temps = pulp.LpVariable("MinTempsHebdo", lowBound=0, cat='Integer')

    plannings_autorises = {"250": ["Planning 250", "Planning 100", "Planning 50"], "100": ["Planning 100", "Planning 50"], "50": ["Planning 50"]}
    slots_dans_24h = int((24 * 60) / resolution)
    creneaux_par_jour = {jour: [c for c in creneaux_globaux if c.startswith(jour)] for jour in jours_semaine}
        
    for j in joueurs:
        prob += Temps_Total[j] == pulp.lpSum(Y[j, c] for c in creneaux_globaux), f"Calc_Temps_{j}"
        prob += Max_Temps >= Temps_Total[j], f"Def_Max_{j}"
        prob += Min_Temps <= Temps_Total[j], f"Def_Min_{j}"

        d_joueur = dict_joueurs.get(j)
        max_h_hebdo = d_joueur.get("heures_max_hebdo", 100)
        prob += Temps_Total[j] <= int((max_h_hebdo * 60) / resolution), f"Limite_Heures_Hebdo_{j}"

        for jour, creneaux_j in creneaux_par_jour.items():
            dispos_j = dict_dispos_jour.get(f"{j}_{jour}", [])
            repas_vus = set()
            for d_jour_data in dispos_j:
                r_duree = d_jour_data.get("repas_duree", 0)
                if r_duree > 0:
                    r_debut = d_jour_data.get("repas_debut", "11:00")
                    r_fin = d_jour_data.get("repas_fin", "15:00")
                    cle_repas = (r_debut, r_fin, r_duree)
                    if cle_repas not in repas_vus:
                        repas_vus.add(cle_repas)
                        slots_repas = int(r_duree / resolution)
                        creneaux_fenetre = [c for c in creneaux_j if r_debut <= c.split("_")[1] < (r_fin if r_fin != "23:59" else "24:00")]
                        if len(creneaux_fenetre) > slots_repas:
                            max_work_slots = len(creneaux_fenetre) - slots_repas
                            prob += pulp.lpSum(Y[j, c] for c in creneaux_fenetre) <= max_work_slots, f"Repas_{j}_{jour}_{r_debut}"
                        elif len(creneaux_fenetre) > 0:
                            prob += pulp.lpSum(Y[j, c] for c in creneaux_fenetre) == 0, f"RepasBlock_{j}_{jour}_{r_debut}"

        for i in range(len(creneaux_globaux)):
            c_val = creneaux_globaux[i]
            jour_c, h_str = c_val.split("_")
            dispos_j = dict_dispos_jour.get(f"{j}_{jour_c}", [])
            d_cible = next((d for d in dispos_j if d["debut"] <= h_str < (d["fin"] if d["fin"] != "23:59" else "24:00")), None)
            
            if not d_cible: continue

            max_h_24h = d_cible.get("heures_max_jour", 6)
            max_slots_24h = int((max_h_24h * 60) / resolution)
            max_slots = int(d_cible.get("t_max_affile", 120) / resolution)
            min_slots = int(d_cible.get("t_min_base", 60) / resolution)
            break_slots = int(d_cible.get("break_min_heavy", 30) / resolution) 
            autorise_micro = d_cible.get("micro_session_ok", False)

            fenetre_24 = min(slots_dans_24h, len(creneaux_globaux) - i)
            if fenetre_24 > max_slots_24h:
                prob += pulp.lpSum(Y[j, creneaux_globaux[i+k]] for k in range(fenetre_24)) <= max_slots_24h, f"Glissant_24h_{j}_{i}"

            if max_slots > 0 and break_slots > 0:
                fenetre_break = max_slots + break_slots
                if i + fenetre_break <= len(creneaux_globaux):
                    prob += pulp.lpSum(Y[j, creneaux_globaux[i+k]] for k in range(fenetre_break)) <= max_slots, f"Break_{j}_{i}"
            elif max_slots > 0:
                if i + max_slots < len(creneaux_globaux):
                    prob += pulp.lpSum(Y[j, creneaux_globaux[i+k]] for k in range(max_slots + 1)) <= max_slots, f"Max_{j}_{i}"

            if min_slots > 1 and i + min_slots <= len(creneaux_globaux):
                if i == 0:
                    start_var = Y[j, creneaux_globaux[0]]
                else:
                    start_var = Y[j, creneaux_globaux[i]] - Y[j, creneaux_globaux[i-1]]
                    if autorise_micro and i >= 3:
                        start_var -= Y[j, creneaux_globaux[i-3]]
                for k in range(1, min_slots):
                    prob += start_var <= Y[j, creneaux_globaux[i+k]], f"Min_{j}_{i}_{k}"

    h_pause_max = matrice_affinites.get("pause_interdite_min", 6)
    h_nuit_min = matrice_affinites.get("repos_nuit_min", 10)
    slots_pause_max = int(h_pause_max * 60 / resolution)
    slots_nuit_min = int(h_nuit_min * 60 / resolution)
    
    for j in joueurs:
        for i in range(len(creneaux_globaux)):
            for k in range(slots_pause_max + 1, slots_nuit_min + 1):
                if i + k < len(creneaux_globaux):
                    prob += Y[j, creneaux_globaux[i]] + Y[j, creneaux_globaux[i+k]] <= 1 + pulp.lpSum(Y[j, creneaux_globaux[i+m]] for m in range(1, k)), f"NoMidBreak_{j}_{i}_{k}"

    for c in creneaux_globaux:
        for j in joueurs:
            prob += Y[j, c] == pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS), f"Lien_XY_{j}_{c}"
            prob += pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS) <= 1, f"Unicite_{j}_{c}"

    objectif = []
    objectif.append(1000 * pulp.lpSum(Y[j, c] for j in joueurs for c in creneaux_globaux))
    objectif.append(-100 * (Max_Temps - Min_Temps)) 

    for j in joueurs:
        for c_global in creneaux_globaux:
            jour, h_str = c_global.split("_")
            dispos_j = dict_dispos_jour.get(f"{j}_{jour}", [])
            d_jour = next((d for d in dispos_j if d["debut"] <= h_str < (d["fin"] if d["fin"] != "23:59" else "24:00")), None)
            
            if d_jour:
                limite_joueur = str(d_jour.get("limite_max", 250))
                prefs_admin = matrice_affinites.get(limite_joueur, {"Planning 250": 1, "Planning 100": 1, "Planning 50": 1})
                autorises = plannings_autorises.get(limite_joueur, PLANNINGS_DISPOS)
                for p in PLANNINGS_DISPOS:
                    if p not in autorises:
                        prob += X[j, p, c_global] == 0, f"Interdit_{j}_{p}_{c_global}"
                    else:
                        objectif.append((prefs_admin.get(p, 1) * 20) * X[j, p, c_global])
            else:
                prob += Y[j, c_global] == 0, f"Indispo_{j}_{c_global}"
            
    prob += pulp.lpSum(objectif)

    for c in creneaux_globaux:
        for p in PLANNINGS_DISPOS:
            prob += pulp.lpSum(X[j, p, c] for j in joueurs) <= 1, f"Cap_{p}_{c}"

    for force in assignations_forcees:
        c_cible = f"{force['jour']}_{force['heure']}"
        if c_cible in creneaux_globaux and force['nom'] in joueurs:
            prob += X[force['nom'], force['planning'], c_cible] == 1, f"Force_{force['nom']}_{c_cible}"

    prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit, gapRel=gap_rel))
    
    planning_final = []
    temps_hebdo = {j: 0 for j in joueurs}
    
    if pulp.LpStatus[prob.status] in ['Optimal', 'Not Solved']:
        for c_global in creneaux_globaux:
            jour, h_str = c_global.split("_")
            for p in PLANNINGS_DISPOS:
                joueurs_assignes = [j for j in joueurs if pulp.value(X[j, p, c_global]) == 1]
                if joueurs_assignes:
                    planning_final.append({"Jour": jour, "Horaire": h_str, "Planning": p, "Joueurs_Liste": joueurs_assignes})
                    for j in joueurs_assignes:
                        temps_hebdo[j] += resolution
    return planning_final, temps_hebdo, pulp.LpStatus[prob.status]

# --- FONCTION DE LISSAGE ---
def lisser_planning(planning_brut, donnees_totales, resolution):
    if not planning_brut: return []
    plannings_autorises = {"250": ["Planning 250", "Planning 100", "Planning 50"], "100": ["Planning 100", "Planning 50"], "50":  ["Planning 50"]}
    joueur_limite = {d["nom"]: str(d.get("limite_max", 250)) for d in donnees_totales}

    grille = {}
    for ligne in planning_brut:
        cle = (ligne["Jour"], ligne["Horaire"])
        if cle not in grille: grille[cle] = {}
        grille[cle][ligne["Planning"]] = ligne["Joueurs_Liste"][0]

    ordre_jours = {"Lundi":0, "Mardi":1, "Mercredi":2, "Jeudi":3, "Vendredi":4, "Samedi":5, "Dimanche":6}
    cles_triees = sorted(grille.keys(), key=lambda x: (ordre_jours.get(x[0], 7), x[1]))
    etat_precedent = {} 
    
    for cle in cles_triees:
        jour_actuel, heure_actuelle = cle
        h_obj_actuel = datetime.strptime(heure_actuelle, '%H:%M')
        plannings_occupes = list(grille[cle].keys())
        joueurs_presents = list(grille[cle].values())
        meilleur_score = -1
        meilleure_dispo = None
        
        for permutation in itertools.permutations(joueurs_presents):
            valide = True
            score = 0
            for i, joueur in enumerate(permutation):
                planning_cible = plannings_occupes[i]
                limite_j = joueur_limite.get(joueur, "250")
                autorises_j = plannings_autorises.get(limite_j, ["Planning 250", "Planning 100", "Planning 50"])
                
                if planning_cible not in autorises_j:
                    valide = False
                    break
                    
                etat_prec = etat_precedent.get(joueur)
                if etat_prec and etat_prec["planning"] == planning_cible and etat_prec["jour"] == jour_actuel:
                    h_prec = datetime.strptime(etat_prec["heure"], '%H:%M')
                    if h_prec + timedelta(minutes=resolution) == h_obj_actuel:
                        score += 1 
                    
            if valide and score > meilleur_score:
                meilleur_score = score
                meilleure_dispo = permutation
        
        if meilleure_dispo:
            for i, joueur in enumerate(meilleure_dispo):
                grille[cle][plannings_occupes[i]] = joueur
                etat_precedent[joueur] = {"planning": plannings_occupes[i], "jour": jour_actuel, "heure": heure_actuelle}

    planning_lisse = []
    for (jour, horaire), affectations in grille.items():
        for planning, joueur in affectations.items():
            planning_lisse.append({"Jour": jour, "Horaire": horaire, "Planning": planning, "Joueurs_Liste": [joueur]})
    return planning_lisse

# --- GÉNÉRATEUR VISUEL ---
def generer_grille_html(planning, joueurs_uniques, resolution, joueur_cible=None):
    if not planning: return "<p>Aucun planning généré.</p>"
    n_joueurs = len(joueurs_uniques)
    map_couleurs = {}
    
    for i, joueur in enumerate(joueurs_uniques):
        hue = i / n_joueurs if n_joueurs > 0 else 0
        r, g, b = colorsys.hls_to_rgb(hue, 0.85, 0.8)
        map_couleurs[joueur] = f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"
    
    ordre_jours = {"Lundi":0, "Mardi":1, "Mercredi":2, "Jeudi":3, "Vendredi":4, "Samedi":5, "Dimanche":6}
    jours = sorted(list(set([p["Jour"] for p in planning])), key=lambda j: ordre_jours.get(j, 7))
    horaires_str = sorted(list(set([p["Horaire"] for p in planning])))
    couleurs_colonnes = {"Planning 250": "#ffebee", "Planning 100": "#e8f5e9", "Planning 50": "#e3f2fd"}
    
    html = "<table style='width:100%; border-collapse: collapse; text-align: center; font-family: sans-serif; color: #333;'>"
    html += "<tr><th rowspan='2' style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>Horaire</th>"
    for j in jours: html += f"<th colspan='3' style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>{j}</th>"
    html += "</tr><tr>"
    for j in jours:
        for p in ["Planning 250", "Planning 100", "Planning 50"]: html += f"<th style='border: 1px solid #ddd; padding: 5px; background-color: {couleurs_colonnes[p]};'>{p.split(' ')[1]}</th>"
    html += "</tr>"
    
    for h_str in horaires_str:
        h_obj = datetime.strptime(h_str, '%H:%M')
        plage_horaire = f"{h_str} - {(h_obj + timedelta(minutes=resolution)).strftime('%H:%M')}"
        html += f"<tr><td style='border: 1px solid #ddd; padding: 8px; font-weight: bold; white-space: nowrap; background-color: #fafafa;'>{plage_horaire}</td>"
        
        for j in jours:
            for p in ["Planning 250", "Planning 100", "Planning 50"]:
                slot = next((item for item in planning if item["Jour"] == j and item["Horaire"] == h_str and item["Planning"] == p), None)
                html += f"<td style='border: 1px solid #ddd; padding: 4px; background-color: {couleurs_colonnes[p]};'>"
                if slot and slot["Joueurs_Liste"]:
                    for joueur in slot["Joueurs_Liste"]:
                        if joueur_cible and joueur != joueur_cible: continue
                        c = map_couleurs.get(joueur, "#eee")
                        html += f"<div style='background-color: {c}; color: #000; margin: 2px; padding: 4px; border-radius: 6px; font-size: 0.85em; font-weight: 500;'>{joueur}</div>"
                html += "</td>"
        html += "</tr>"
    html += "</table>"
    return html

# ==========================================
# INTERFACE UTILISATEUR
# ==========================================

donnees_globales = charger_donnees()

st.title("Générateur de Planning Unibet Multi-Limites 🗓️")

with st.sidebar:
    st.header("👑 Accès Admin")
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "Romarino7") 
    
    # NOUVEAU : Sélecteur de semaine pour l'admin
    if est_admin:
        st.markdown("---")
        st.header("📅 Navigation Semaines")
        admin_annee = st.number_input("Année Cible", min_value=2024, max_value=2030, value=target_year)
        admin_semaine = st.number_input("Semaine ISO Cible", min_value=1, max_value=53, value=target_week)

# --- VUE 1 : LES JOUEURS ---
if not est_admin:
    st.header("👤 Espace Joueur")
    nom_joueur = st.text_input("Identifiez-vous (Pseudo) :")
    
    if nom_joueur.strip():
        st.markdown(f"### Bienvenue {nom_joueur} !")
        st.write(f"📅 **Préparation de la Semaine {target_week}** (Année {target_year})")
        
        # --- LE CHRONOMÈTRE JS ---
        if not is_locked:
            compte_a_rebours_html = f"""
            <div style="padding: 15px; border-radius: 8px; background-color: #e8f5e9; border: 1px solid #c8e6c9; color: #2e7d32; text-align: center; font-size: 18px; margin-bottom: 20px;">
                ⏳ <b>Clôture des dispos (Samedi 12h00) :</b> <span id="countdown" style="font-weight: bold; font-family: monospace;"></span>
                <script>
                    var countDownDate = new Date("{deadline_dt.isoformat()}").getTime();
                    var x = setInterval(function() {{
                        var now = new Date().getTime();
                        var distance = countDownDate - now;
                        if (distance < 0) {{
                            clearInterval(x);
                            document.getElementById("countdown").innerHTML = "EXPIRÉE";
                        }} else {{
                            var days = Math.floor(distance / (1000 * 60 * 60 * 24));
                            var hours = Math.floor((distance % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
                            var minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
                            var seconds = Math.floor((distance % (1000 * 60)) / 1000);
                            document.getElementById("countdown").innerHTML = days + "j " + hours + "h " + minutes + "m " + seconds + "s";
                        }}
                    }}, 1000);
                </script>
            </div>
            """
            st.markdown(compte_a_rebours_html, unsafe_allow_html=True)
        else:
            st.error("🔒 **La deadline est dépassée.** Le formulaire est verrouillé pendant que l'algorithme génère le planning. Vous pourrez entrer vos prochaines disponibilités dès Lundi !")

        # On cache le formulaire si la deadline est dépassée
        if not is_locked:
            with st.form("formulaire_dispo", clear_on_submit=False):
                st.markdown("#### 🎯 Paramètres du Joueur")
                
                st.info("ℹ️ **Légende des paramètres :**\n"
                        "- 🌍 **Globaux** (appliqués à toute la semaine) : *Limite Max* et *Max d'heures / Semaine*.\n"
                        "- 📌 **Par créneau** (spécifiques à la dispo) : *Max 24h, rythme, repas, micro-session*.")
                
                limite_max = st.selectbox("Sélectionnez votre Limite Max (🌍 Global)", [250, 100, 50])
                
                st.markdown("#### ⚙️ Contraintes de rythme")
                c_h, c_j = st.columns(2)
                with c_h: heures_max_hebdo = st.number_input("Maximum d'heures / SEMAINE (🌍 Global)", value=100, step=1)
                with c_j: heures_max_jour = st.number_input("Maximum d'heures / 24H (Glissant) (📌)", value=6, step=1)
                
                c1, c2 = st.columns(2)
                with c1:
                    temps_max_affile = st.number_input("Max temps d'affilée (min) (📌)", value=120, step=30)
                    creneau_min_base = st.number_input("Temps minimum par session (📌)", value=60, step=30)
                with c2: 
                    break_min_heavy = st.number_input("Pause min après grosse session (📌)", value=60, step=15)

                st.markdown("#### ⚡ Exception : Micro-session")
                micro_session_ok = st.checkbox("Autoriser une session courte (30min) à la suite d'un break court (30 min)", value=True)
                    
                liste_heures = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)] + ["23:59"]

                st.markdown("#### 🍔 Pause Repas")
                st.write("Indiquez la plage horaire dans laquelle vous souhaitez manger et la durée nécessaire. *(Laissez à 0 si vous pouvez vous adapter)*")
                col_r1, col_r2, col_r3 = st.columns(3)
                with col_r1: repas_debut = st.selectbox("Début fenêtre repas", liste_heures, index=liste_heures.index("11:00"))
                with col_r2: repas_fin = st.selectbox("Fin fenêtre repas", liste_heures, index=liste_heures.index("15:00"))
                with col_r3: repas_duree = st.number_input("Durée repas (min)", value=0, step=30)

                st.markdown("---")
                st.subheader("Ajouter une disponibilité")
                jours_choisis = st.multiselect("Jours concernés", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
                
                col1, col2 = st.columns(2)
                with col1: debut_str = st.selectbox("Arrivée", liste_heures, index=liste_heures.index("18:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
                with col2: fin_str = st.selectbox("Départ", liste_heures, index=liste_heures.index("22:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
                    
                btn_ajouter = st.form_submit_button("Enregistrer pour ces jours")
                
                st.markdown("---")
                st.markdown("#### 🔄 Mettre à jour mon profil")
                st.write("Un changement de rythme ou de repas ? Appliquez vos paramètres actuels à tous vos créneaux existants d'un coup.")
                btn_mettre_a_jour = st.form_submit_button("Mettre à jour tous mes anciens créneaux", type="secondary")

            # --- GESTION DES BOUTONS DE SOUVEGARDE ---
            if btn_ajouter:
                # Double sécurité anti-fraude temporelle :
                if datetime.now(ZoneInfo("Europe/Paris")) >= deadline_dt:
                    st.error("⏳ Deadline dépassée à l'instant, enregistrement annulé.")
                elif not jours_choisis:
                    st.error("Veuillez sélectionner au moins un jour.")
                else:
                    for j in jours_choisis:
                        nouvelle_dispo = {
                            "id": str(uuid.uuid4()),
                            "nom": nom_joueur.strip(), "jour": j,
                            "debut": debut_str, "fin": fin_str,
                            "limite_max": limite_max,
                            "t_max_affile": temps_max_affile,
                            "t_min_base": creneau_min_base,
                            "break_min_heavy": break_min_heavy,
                            "heures_max_hebdo": heures_max_hebdo,
                            "heures_max_jour": heures_max_jour,
                            "micro_session_ok": micro_session_ok,
                            "repas_debut": repas_debut,
                            "repas_fin": repas_fin,
                            "repas_duree": repas_duree,
                            "semaine_cible": target_week, # NOUVEAU: Le tampon temporel
                            "annee_cible": target_year
                        }
                        ajouter_dispo(nouvelle_dispo)
                    st.success(f"Créneaux ajoutés pour la Semaine {target_week} !")
                    st.rerun()

            if btn_mettre_a_jour:
                if datetime.now(ZoneInfo("Europe/Paris")) >= deadline_dt:
                    st.error("⏳ Deadline dépassée.")
                else:
                    # Ne met à jour que la semaine cible
                    creneaux_joueur = [d for d in donnees_globales if d["nom"] == nom_joueur.strip() and d.get('semaine_cible', target_week) == target_week and d.get('annee_cible', target_year) == target_year]
                    if not creneaux_joueur:
                        st.warning(f"Vous n'avez aucun créneau enregistré pour la semaine {target_week} à mettre à jour.")
                    else:
                        with st.spinner("Mise à jour en cours..."):
                            for d in creneaux_joueur:
                                d["limite_max"] = limite_max
                                d["heures_max_hebdo"] = heures_max_hebdo
                                d["heures_max_jour"] = heures_max_jour
                                d["t_max_affile"] = temps_max_affile
                                d["t_min_base"] = creneau_min_base
                                d["break_min_heavy"] = break_min_heavy
                                d["micro_session_ok"] = micro_session_ok
                                d["repas_debut"] = repas_debut
                                d["repas_fin"] = repas_fin
                                d["repas_duree"] = repas_duree
                                
                                for old_key in ["break_max_cond", "t_min_adj", "intervalle_nuit"]:
                                    if old_key in d: del d[old_key]
                                    
                                ajouter_dispo(d)
                        st.success(f"✅ Tous vos créneaux de la semaine {target_week} sont à jour !")
                        st.rerun()

        # --- SECTION : AFFICHAGE DU PLANNING OFFICIEL ---
        planning_officiel, res_officielle, is_visible = charger_planning_officiel(annee=target_year, semaine=target_week)
        
        if planning_officiel and is_visible:
            st.markdown("---")
            st.subheader(f"📅 Planning Officiel (Semaine {target_week})")
            
            is_solo = st.toggle("👀 Mettre en évidence uniquement mes créneaux", value=False)
            
            heures_joueur = sum([res_officielle/60 for p in planning_officiel if nom_joueur.strip() in p['Joueurs_Liste']])
            if is_solo and heures_joueur > 0:
                st.info(f"💡 Vous avez **{heures_joueur} heures** de jeu prévues au total cette semaine.")
            
            tous_les_joueurs_planning = list(set([j for p in planning_officiel for j in p['Joueurs_Liste']]))
            
            html_planning = generer_grille_html(planning_officiel, tous_les_joueurs_planning, res_officielle, joueur_cible=nom_joueur.strip() if is_solo else None)
            st.markdown(html_planning, unsafe_allow_html=True)
            
        elif planning_officiel and not is_visible:
            st.markdown("---")
            st.info("Le planning a été temporairement masqué par l'administrateur.")

        st.markdown("---")
        st.subheader(f"Vos disponibilités (Semaine {target_week})")
        
        # Filtre d'affichage uniquement sur la semaine cible
        mes_dispos = [d for d in donnees_globales if d["nom"] == nom_joueur.strip() and d.get('semaine_cible', target_week) == target_week and d.get('annee_cible', target_year) == target_year]
        
        if mes_dispos:
            ordre_jours_ui = {"Lundi":0, "Mardi":1, "Mercredi":2, "Jeudi":3, "Vendredi":4, "Samedi":5, "Dimanche":6}
            mes_dispos_tries = sorted(mes_dispos, key=lambda d: (ordre_jours_ui.get(d['jour'], 7), d['debut']))

            for d in mes_dispos_tries:
                col_info, col_btn = st.columns([10, 1])
                with col_info:
                    fin_affichee = "Minuit" if d['fin'] == "23:59" else d['fin']
                    parts = [
                        f"**{d['jour']} {d['debut']}-{fin_affichee}**",
                        f"💰 L{d.get('limite_max', '-')}",
                        f"⏱️ {d.get('heures_max_hebdo', '-')}h/sem",
                        f"24H: {d.get('heures_max_jour', '-')}h",
                        f"Max: {d.get('t_max_affile', '-')}m",
                        f"Min: {d.get('t_min_base', '-')}m",
                        f"Brk: {d.get('break_min_heavy', '-')}m"
                    ]
                    if d.get('repas_duree', 0) > 0:
                        parts.append(f"🍔 {d.get('repas_duree')}m ({d.get('repas_debut')}-{d.get('repas_fin')})")
                    if d.get('micro_session_ok', False):
                        parts.append("⚡ Micro: Oui")
                        
                    st.markdown(" | ".join(parts))
                
                with col_btn:
                    # Ne permet la suppression que si la deadline n'est pas passée
                    if not is_locked:
                        if st.button("❌", key=d["id"]):
                            supprimer_entree(d["id"])
                            st.rerun()
                    else:
                        st.markdown("🔒")

# --- VUE 2 : L'ADMINISTRATEUR ---
if est_admin:
    st.success(f"Mode Administrateur activé - Vous gérez la Semaine {admin_semaine} ({admin_annee})")
    
    # NOUVEAU : On filtre drastiquement toutes les données de l'Admin selon la semaine sélectionnée
    donnees_semaine = [d for d in donnees_globales if d.get('semaine_cible', target_week) == admin_semaine and d.get('annee_cible', target_year) == admin_annee]
    noms_dispos = list(set([d["nom"] for d in donnees_semaine])) if donnees_semaine else []
    
    if st.button("🗑️ Effacer TOUTES les données de cette base (Toutes semaines)"):
        vider_toutes_les_dispos()
        st.session_state.assignations_forcees = []
        sauvegarder_matrice_affinites({
            "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
            "100": {"Planning 250": 1, "Planning 100": 3, "Planning 50": 3},
            "50": {"Planning 250": 1, "Planning 100": 1, "Planning 50": 3},
            "pause_interdite_min": 6,
            "repos_nuit_min": 10
        })
        st.rerun()

    tab_liste, tab_force, tab_param, tab_gen = st.tabs(["📋 Liste des Créneaux", "🛠️ Assignations", "⚙️ Paramètres", "🚀 Génération & Planning"])

    with tab_liste:
        if donnees_semaine:
            df = pd.DataFrame(donnees_semaine)
            
            colonnes_a_ignorer = ["intervalle_nuit", "break_max_cond", "t_min_adj", "semaine_cible", "annee_cible"]
            df = df.drop(columns=[c for c in colonnes_a_ignorer if c in df.columns])
            
            if not df.empty:
                cols = df.columns.tolist()
                first_cols = ['nom', 'jour', 'debut', 'fin', 'limite_max', 'heures_max_hebdo', 'heures_max_jour', 't_max_affile', 't_min_base', 'break_min_heavy', 'micro_session_ok', 'repas_debut', 'repas_fin', 'repas_duree']
                
                for fc in first_cols:
                    if fc not in df.columns: df[fc] = None
                        
                first_cols = [c for c in first_cols if c in df.columns]
                other_cols = [c for c in cols if c not in first_cols and c != 'id']
                df = df[first_cols + other_cols]
                
                df = df.sort_values(by=['nom', 'jour', 'debut']).reset_index(drop=True)
                st.dataframe(df, use_container_width=True)
                
                st.markdown("### 🗑️ Suppression de créneaux")
                joueurs_liste = sorted(df['nom'].unique().tolist())
                joueur_filtre = st.selectbox("1. Filtrer par joueur :", ["Tous"] + joueurs_liste)
                
                dispos_filtrees = [d for d in donnees_semaine if joueur_filtre == "Tous" or d["nom"] == joueur_filtre]
                options_suppression = {f"{d['nom']} | {d['jour']} {d['debut']}-{d['fin']}": d['id'] for d in dispos_filtrees}
                
                creneaux_a_supprimer = st.multiselect("2. Sélectionner les créneaux à supprimer :", list(options_suppression.keys()))
                
                if st.button("🗑️ Supprimer la sélection", type="primary") and creneaux_a_supprimer:
                    for sel in creneaux_a_supprimer: supprimer_entree(options_suppression[sel])
                    st.rerun()

    with tab_force:
        st.subheader("Forcer des assignations manuelles")
        col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns([2, 2, 2, 2, 1])
        with col_f1: f_jour = st.selectbox("Jour", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
        with col_f2: f_heure = st.time_input("Heure", value=pd.to_datetime("18:00").time(), step=timedelta(minutes=30))
        with col_f3: f_joueur = st.selectbox("Joueur", noms_dispos if noms_dispos else ["Aucun"])
        with col_f4: f_planning = st.selectbox("Planning", PLANNINGS_DISPOS)
        with col_f5:
            st.write(""); st.write("")
            if st.button("➕ Ajouter") and f_joueur != "Aucun":
                st.session_state.assignations_forcees.append({"jour": f_jour, "heure": f_heure.strftime("%H:%M"), "nom": f_joueur, "planning": f_planning})
                st.success("Ajouté")
        
        if st.session_state.assignations_forcees:
            for r in st.session_state.assignations_forcees:
                st.caption(f"- {r['nom']} le {r['jour']} à {r['heure']} sur {r['planning']}")
            if st.button("Effacer les règles"):
                st.session_state.assignations_forcees = []
                st.rerun()

    with tab_param:
        st.subheader("Matrice des Affinités et Pauses")
        affinites_admin = charger_matrice_affinites()
        
        with st.form("form_affinites"):
            st.markdown("**Pour les joueurs avec Limite Max = 250 :**")
            col_a1, col_b1, col_c1 = st.columns(3)
            with col_a1: val_250_250 = st.slider("Planning 250", 1, 5, affinites_admin.get("250", {}).get("Planning 250", 3), key="s_250_250")
            with col_b1: val_100_250 = st.slider("Planning 100", 1, 5, affinites_admin.get("250", {}).get("Planning 100", 3), key="s_100_250")
            with col_c1: val_50_250 = st.slider("Planning 50", 1, 5, affinites_admin.get("250", {}).get("Planning 50", 3), key="s_50_250")
            affinites_admin["250"] = {"Planning 250": val_250_250, "Planning 100": val_100_250, "Planning 50": val_50_250}
            
            st.markdown("**Pour les joueurs avec Limite Max = 100 :**")
            col_a2, col_b2 = st.columns(2)
            with col_a2: val_100_100 = st.slider("Planning 100", 1, 5, affinites_admin.get("100", {}).get("Planning 100", 3), key="s_100_100")
            with col_b2: val_50_100 = st.slider("Planning 50", 1, 5, affinites_admin.get("100", {}).get("Planning 50", 3), key="s_50_100")
            affinites_admin["100"] = {"Planning 250": 1, "Planning 100": val_100_100, "Planning 50": val_50_100}

            st.markdown("**Pour les joueurs avec Limite Max = 50 :**")
            val_50_50 = st.slider("Planning 50", 1, 5, affinites_admin.get("50", {}).get("Planning 50", 3), key="s_50_50")
            affinites_admin["50"] = {"Planning 250": 1, "Planning 100": 1, "Planning 50": val_50_50}

            st.markdown("---")
            st.markdown("**🛌 Paramètres des Cycles de Repos :**")
            c_p1, c_p2 = st.columns(2)
            with c_p1: pause_interdite = st.number_input("Début Zone Interdite (Max pause courte)", value=affinites_admin.get("pause_interdite_min", 6), step=1)
            with c_p2: repos_nuit = st.number_input("Fin Zone Interdite (Vraie nuit min)", value=affinites_admin.get("repos_nuit_min", 10), step=1)
                
            affinites_admin["pause_interdite_min"] = pause_interdite
            affinites_admin["repos_nuit_min"] = repos_nuit
                
            if st.form_submit_button("Enregistrer la configuration", type="primary"):
                if pause_interdite >= repos_nuit: st.error("Le début de la zone interdite doit être strictement inférieur à la vraie nuit.")
                else:
                    sauvegarder_matrice_affinites(affinites_admin)
                    st.success("Configuration enregistrée !")

    with tab_gen:
        st.subheader("Lancer l'Optimisation Mathématique")
        c_res, c_gap, c_time = st.columns(3)
        with c_res: resolution = st.number_input("Résolution (min)", value=30, step=5)
        with c_gap: gap_rel_input = st.number_input("Tolérance Solveur (gapRel)", min_value=0.0, max_value=1.0, value=0.005, step=0.005, format="%.3f")
        with c_time: time_limit_input = st.number_input("Temps Max Solveur (sec)", min_value=60, value=600, step=60)

        if st.button("🚀 Résoudre la semaine entière", type="primary"):
            if not donnees_semaine:
                st.error("Aucune donnée pour cette semaine.")
            else:
                matrice = charger_matrice_affinites()
                with st.spinner(f"Génération du planning avec contraintes dynamiques (Tolérance {gap_rel_input*100}%)..."):
                    
                    start_time = time.time()
                    # On envoie QUE les données de la semaine sélectionnée au solveur
                    planning_brut, temps_totaux, statut_solveur = optimiser_planning_hebdo(
                        donnees_semaine, resolution, 
                        st.session_state.assignations_forcees, matrice, gap_rel=gap_rel_input, time_limit=time_limit_input
                    )
                    end_time = time.time()
                    duree_resolution = end_time - start_time
                    
                    planning_complet = lisser_planning(planning_brut, donnees_semaine, resolution) if planning_brut else []
                        
                    st.session_state.planning_complet = planning_complet
                    st.session_state.temps_totaux = temps_totaux
                    st.session_state.statut_solveur = statut_solveur
                    st.session_state.duree_resolution = duree_resolution

        if 'planning_complet' in st.session_state and st.session_state.planning_complet:
            st.markdown("---")
            if st.session_state.statut_solveur == 'Optimal':
                st.success(f"✅ **Statut Optimal (Marge de {gap_rel_input*100}%)** : Le meilleur planning possible a été trouvé rapidement. *(Temps de résolution : {st.session_state.duree_resolution:.2f}s)*")
            elif st.session_state.statut_solveur == 'Not Solved':
                st.warning(f"⏱️ **Temps écoulé ({time_limit_input} sec)** : Un excellent planning a été trouvé, mais le solveur a été coupé.")
            else:
                st.error(f"⚠️ Statut inhabituel du solveur : {st.session_state.statut_solveur}")
            
            st.markdown("### 📅 Emploi du temps")
            st.markdown(generer_grille_html(st.session_state.planning_complet, noms_dispos, resolution), unsafe_allow_html=True)
            
            st.markdown("### 📊 Temps de jeu total (Hebdomadaire)")
            stats_data = [{"Joueur": j, "Temps (Heures)": t/60.0} for j, t in st.session_state.temps_totaux.items() if t > 0]
            if stats_data:
                df_stats = pd.DataFrame(stats_data)
                barres = alt.Chart(df_stats).mark_bar().encode(x=alt.X('Joueur:O', sort='-y'), y='Temps (Heures):Q')
                st.altair_chart(barres, use_container_width=True)

            st.markdown("---")
            st.markdown("### ☁️ Publication en ligne")
            
            col_pub1, col_pub2, col_pub3 = st.columns(3)
            with col_pub1:
                if st.button("📢 Partager aux joueurs", type="primary"):
                    with st.spinner("Publication du planning..."):
                        publier_planning_officiel(st.session_state.planning_complet, resolution, visible=True, annee=admin_annee, semaine=admin_semaine)
                        st.success("✅ Planning partagé !")
            with col_pub2:
                if st.button("🙈 Masquer aux joueurs", type="secondary"):
                    with st.spinner("Mise à jour..."):
                        publier_planning_officiel(st.session_state.planning_complet, resolution, visible=False, annee=admin_annee, semaine=admin_semaine)
                        st.info("Le planning est maintenant caché.")
                        
            with col_pub3:
                if st.button("🌐 MàJ le Google Sheet", type="secondary"):
                    with st.spinner("Envoi des données..."):
                        try:
                            mettre_a_jour_google_sheet(st.session_state.planning_complet, resolution)
                            st.success("✅ Le Google Sheet a été mis à jour avec succès !")
                            st.markdown("[Lien vers le Google Sheet public](https://docs.google.com/spreadsheets/d/1wl_RLPs1h7TsUQFDQj6An0ouhU1-KlXEmBURksGDPic/edit)")
                        except Exception as e:
                            st.error(f"Erreur lors de la mise à jour : {e}")
