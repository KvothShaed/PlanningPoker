import streamlit as st
import pandas as pd
import uuid
import pulp
import altair as alt
from datetime import datetime, timedelta
import colorsys
import gspread
from google.oauth2.service_account import Credentials

# --- NOUVEAUX IMPORTS POUR FIREBASE ---
import firebase_admin
from firebase_admin import credentials, firestore

st.set_page_config(page_title="Planning Optimisé & Multisites", layout="wide")

PLANNINGS_DISPOS = ["Planning 250", "Planning 100", "Planning 50"]

if 'assignations_forcees' not in st.session_state:
    st.session_state.assignations_forcees = []

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

def charger_donnees():
    docs = db.collection('dispos').stream()
    return [doc.to_dict() for doc in docs]

def ajouter_dispo(dispo_data):
    db.collection('dispos').document(dispo_data["id"]).set(dispo_data)

def supprimer_entree(id_entree):
    db.collection('dispos').document(id_entree).delete()

def vider_toutes_les_dispos():
    docs = db.collection('dispos').stream()
    for doc in docs:
        doc.reference.delete()

def charger_matrice_affinites():
    doc = db.collection('config').document('affinites').get()
    if doc.exists:
        return doc.to_dict()
    return {
        "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
        "100": {"Planning 250": 1, "Planning 100": 3, "Planning 50": 3},
        "50": {"Planning 250": 1, "Planning 100": 1, "Planning 50": 3},
        "repos_nuit_min": 10
    }

def sauvegarder_matrice_affinites(data):
    db.collection('config').document('affinites').set(data)


# --- MOTEUR D'OPTIMISATION MATHÉMATIQUE HEBDOMADAIRE (PuLP) ---
def optimiser_planning_hebdo(donnees_totales, resolution, joueurs_simultanes, assignations_forcees, matrice_affinites):
    if not donnees_totales:
        return [], {}

    prob = pulp.LpProblem("Optimisation_Hebdomadaire", pulp.LpMaximize)
    
    jours_semaine = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    creneaux_globaux = []
    
    for jour in jours_semaine:
        actuel = datetime.strptime("00:00", "%H:%M")
        fin = datetime.strptime("23:59", "%H:%M")
        while actuel < fin:
            creneaux_globaux.append(f"{jour}_{actuel.strftime('%H:%M')}")
            actuel += timedelta(minutes=resolution)

    joueurs = list(set([d["nom"] for d in donnees_totales]))
    
    X = pulp.LpVariable.dicts("Assign", ((j, p, c) for j in joueurs for p in PLANNINGS_DISPOS for c in creneaux_globaux), cat='Binary')
    Y = pulp.LpVariable.dicts("Joue", ((j, c) for j in joueurs for c in creneaux_globaux), cat='Binary')
    
    # NOUVELLE VARIABLE : Début de nuit
    SleepStart = pulp.LpVariable.dicts("SleepStart", ((j, d, c) for j in joueurs for d in jours_semaine for c in creneaux_globaux), cat='Binary')

    Temps_Total = pulp.LpVariable.dicts("TempsTotalHebdo", joueurs, lowBound=0, cat='Integer')
    Max_Temps = pulp.LpVariable("MaxTempsHebdo", lowBound=0, cat='Integer')
    Min_Temps = pulp.LpVariable("MinTempsHebdo", lowBound=0, cat='Integer')

    plannings_autorises = {
        "250": ["Planning 250", "Planning 100", "Planning 50"],
        "100": ["Planning 100", "Planning 50"],
        "50":  ["Planning 50"]
    }

    creneaux_par_jour = {jour: [] for jour in jours_semaine}
    for c in creneaux_globaux:
        jour_str = c.split("_")[0]
        creneaux_par_jour[jour_str].append(c)
        
    heures_nuit = matrice_affinites.get("repos_nuit_min", 10)
    slots_nuit_standard = int((heures_nuit * 60) / resolution)

    for j in joueurs:
        prob += Temps_Total[j] == pulp.lpSum(Y[j, c] for c in creneaux_globaux), f"Calc_Temps_{j}"
        prob += Max_Temps >= Temps_Total[j], f"Def_Max_{j}"
        prob += Min_Temps <= Temps_Total[j], f"Def_Min_{j}"

        d_joueur = next((d for d in donnees_totales if d["nom"] == j), None)
        if d_joueur:
            max_h_hebdo = d_joueur.get("heures_max_hebdo", 100)
            prob += Temps_Total[j] <= int((max_h_hebdo * 60) / resolution), f"Limite_Heures_Hebdo_{j}"
            
            max_h_jour = d_joueur.get("heures_max_jour", 6)
            max_slots_jour = int((max_h_jour * 60) / resolution)
            for jour, creneaux_j in creneaux_par_jour.items():
                prob += pulp.lpSum(Y[j, c] for c in creneaux_j) <= max_slots_jour, f"Max_Jour_{j}_{jour}"

            # --- LA NOUVELLE LOGIQUE : ANCRAGE STRICT DE 5h ---
            intervalle = d_joueur.get("intervalle_nuit", "00:00 - 05:00")
            couche_min = intervalle.split(" - ")[0]
            
            slots_ancre = int(5 * 60 / resolution)
            marge_glissement = max(0, slots_nuit_standard - slots_ancre)
            
            if slots_nuit_standard > 0:
                for i_jour, jour in enumerate(jours_semaine):
                    if jour == "Dimanche":
                        continue 
                        
                    jour_suivant = jours_semaine[i_jour + 1] 
                    
                    if couche_min < "12:00":
                        c_ancre = f"{jour_suivant}_{couche_min}"
                    else:
                        c_ancre = f"{jour}_{couche_min}"
                        
                    if c_ancre in creneaux_globaux:
                        idx_ancre = creneaux_globaux.index(c_ancre)
                        
                        # Le repos de 10h doit démarrer dans cette fenêtre très stricte
                        v_start = max(0, idx_ancre - marge_glissement)
                        v_end = idx_ancre
                        
                        valid_starts = creneaux_globaux[v_start : v_end + 1]
                        
                        if valid_starts:
                            prob += pulp.lpSum(SleepStart[j, jour, s] for s in valid_starts) == 1, f"Doit_Dormir_{j}_{jour}"
                            
                            for start_slot in valid_starts:
                                s_idx = creneaux_globaux.index(start_slot)
                                for k in range(slots_nuit_standard):
                                    if s_idx + k < len(creneaux_globaux):
                                        prob += Y[j, creneaux_globaux[s_idx + k]] <= 1 - SleepStart[j, jour, start_slot], f"Dort_{j}_{jour}_{start_slot}_{k}"

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
            
            d_jour = next((d for d in donnees_totales if d["nom"] == j and d["jour"] == jour and d["debut"] <= h_str < (d["fin"] if d["fin"] != "23:59" else "24:00")), None)
            
            if d_jour:
                limite_joueur = str(d_jour.get("limite_max", 250))
                prefs_admin = matrice_affinites.get(limite_joueur, {"Planning 250": 1, "Planning 100": 1, "Planning 50": 1})
                autorises = plannings_autorises.get(limite_joueur, PLANNINGS_DISPOS)
                
                for p in PLANNINGS_DISPOS:
                    if p not in autorises:
                        prob += X[j, p, c_global] == 0, f"Interdit_{j}_{p}_{c_global}"
                    else:
                        valeur_pref = prefs_admin.get(p, 1)
                        objectif.append(valeur_pref * X[j, p, c_global])
            else:
                prob += Y[j, c_global] == 0, f"Indispo_{j}_{c_global}"
                
    prob += pulp.lpSum(objectif)

    for c in creneaux_globaux:
        for p in PLANNINGS_DISPOS:
            prob += pulp.lpSum(X[j, p, c] for j in joueurs) <= joueurs_simultanes, f"Cap_{p}_{c}"

    for j in joueurs:
        d_joueur = next((d for d in donnees_totales if d["nom"] == j), None)
        if not d_joueur: continue
            
        max_slots = int(d_joueur["t_max_affile"] / resolution)
        min_slots = int(d_joueur["t_min_base"] / resolution)
        break_slots = int(d_joueur.get("break_min_heavy", 30) / resolution) 
        
        if max_slots > 0 and break_slots > 0 and len(creneaux_globaux) > (max_slots + break_slots):
            fenetre = max_slots + break_slots
            for i in range(len(creneaux_globaux) - fenetre + 1):
                prob += pulp.lpSum(Y[j, creneaux_globaux[i+k]] for k in range(fenetre)) <= max_slots, f"Break_{j}_{i}"
                
        elif max_slots > 0 and len(creneaux_globaux) > max_slots:
            for i in range(len(creneaux_globaux) - max_slots):
                prob += pulp.lpSum(Y[j, creneaux_globaux[i+k]] for k in range(max_slots + 1)) <= max_slots, f"Max_{j}_{i}"
                
        if min_slots > 1:
            for k in range(1, min_slots):
                prob += Y[j, creneaux_globaux[0]] <= Y[j, creneaux_globaux[k]], f"Min_{j}_0_{k}"
            for i in range(1, len(creneaux_globaux) - min_slots + 1):
                start_var = Y[j, creneaux_globaux[i]] - Y[j, creneaux_globaux[i-1]]
                for k in range(1, min_slots):
                    prob += start_var <= Y[j, creneaux_globaux[i+k]], f"Min_{j}_{i}_{k}"

    for force in assignations_forcees:
        c_cible = f"{force['jour']}_{force['heure']}"
        if c_cible in creneaux_globaux and force['nom'] in joueurs:
            p_force = force['planning']
            prob += X[force['nom'], p_force, c_cible] == 1, f"Force_{force['nom']}_{c_cible}"

    prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=60))
    
    planning_final = []
    temps_hebdo = {j: 0 for j in joueurs}
    
    if pulp.LpStatus[prob.status] == 'Optimal' or pulp.LpStatus[prob.status] == 'Not Solved':
        for c_global in creneaux_globaux:
            jour, h_str = c_global.split("_")
            for p in PLANNINGS_DISPOS:
                joueurs_assignes = [j for j in joueurs if pulp.value(X[j, p, c_global]) == 1]
                if joueurs_assignes:
                    planning_final.append({
                        "Jour": jour,
                        "Horaire": h_str,
                        "Planning": p,
                        "Joueurs_Liste": joueurs_assignes
                    })
                    for j in joueurs_assignes:
                        temps_hebdo[j] += resolution

    return planning_final, temps_hebdo

# --- GÉNÉRATEUR VISUEL ---
def generer_grille_html(planning, joueurs_uniques, resolution):
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
    for j in jours:
        html += f"<th colspan='3' style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>{j}</th>"
    html += "</tr><tr>"
    for j in jours:
        html += f"<th style='border: 1px solid #ddd; padding: 5px; background-color: {couleurs_colonnes['Planning 250']};'>250</th>"
        html += f"<th style='border: 1px solid #ddd; padding: 5px; background-color: {couleurs_colonnes['Planning 100']};'>100</th>"
        html += f"<th style='border: 1px solid #ddd; padding: 5px; background-color: {couleurs_colonnes['Planning 50']};'>50</th>"
    html += "</tr>"
    
    for h_str in horaires_str:
        h_obj = datetime.strptime(h_str, '%H:%M')
        h_fin_str = (h_obj + timedelta(minutes=resolution)).strftime('%H:%M')
        plage_horaire = f"{h_str} - {h_fin_str}"
        
        html += f"<tr><td style='border: 1px solid #ddd; padding: 8px; font-weight: bold; white-space: nowrap; background-color: #fafafa;'>{plage_horaire}</td>"
        
        for j in jours:
            for p in ["Planning 250", "Planning 100", "Planning 50"]:
                slot = next((item for item in planning if item["Jour"] == j and item["Horaire"] == h_str and item["Planning"] == p), None)
                bg_color = couleurs_colonnes[p]
                
                html += f"<td style='border: 1px solid #ddd; padding: 4px; background-color: {bg_color};'>"
                if slot and slot["Joueurs_Liste"]:
                    for joueur in slot["Joueurs_Liste"]:
                        c = map_couleurs.get(joueur, "#eee")
                        html += f"<div style='background-color: {c}; color: #000; margin: 2px; padding: 4px; border-radius: 6px; font-size: 0.85em; font-weight: 500;'>{joueur}</div>"
                html += "</td>"
        html += "</tr>"
    html += "</table>"
    return html

def mettre_a_jour_google_sheet(planning, resolution):
    if not planning:
        return
        
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = Credentials.from_service_account_info(dict(st.secrets["google_sheets"]), scopes=scopes)
    client = gspread.authorize(creds)

    sheet_id = "1wl_RLPs1h7TsUQFDQj6An0ouhU1-KlXEmBURksGDPic" 
    sheet = client.open_by_key(sheet_id).sheet1

    df = pd.DataFrame(planning)
    
    if not df.empty:
        df["Joueurs_Liste"] = df["Joueurs_Liste"].apply(lambda x: ", ".join(x) if isinstance(x, list) else x)
        
        df["En_Tete"] = df["Jour"] + " | " + df["Planning"].str.replace("Planning ", "")
        
        df_pivot = df.pivot(index="Horaire", columns="En_Tete", values="Joueurs_Liste").fillna("")
        
        jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
        plannings = ["250", "100", "50"]
        
        colonnes_triees = []
        for j in jours:
            for p in plannings:
                nom_col = f"{j} | {p}"
                if nom_col in df_pivot.columns:
                    colonnes_triees.append(nom_col)
                    
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
        sheet.format("A1:Z1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9} 
        })

# ==========================================
# INTERFACE UTILISATEUR
# ==========================================
st.title("Générateur de Planning Unibet Multi-Limites 🗓️")

with st.sidebar:
    st.header("👑 Accès Admin")
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "Romarino7") 

# --- VUE 1 : LES JOUEURS ---
if not est_admin:
    st.header("👤 Espace Joueur")
    nom_joueur = st.text_input("Identifiez-vous (Pseudo) :")
    
    if nom_joueur.strip():
        st.markdown(f"### Bienvenue {nom_joueur} !")
        
        with st.form("formulaire_dispo", clear_on_submit=False):
            st.markdown("#### 🎯 Paramètres du Joueur")
            limite_max = st.selectbox("Sélectionnez votre Limite Max", [250, 100, 50])
            
            st.markdown("#### ⚙️ Contraintes de rythme")
            
            c_h, c_j = st.columns(2)
            with c_h:
                heures_max_hebdo = st.number_input("Maximum d'heures / SEMAINE", value=100, step=1)
            with c_j:
                heures_max_jour = st.number_input("Maximum d'heures / JOUR", value=6, step=1)
            
            c1, c2 = st.columns(2)
            with c1:
                temps_max_affile = st.number_input("Max temps d'affilée (min)", value=120, step=30)
                creneau_min_base = st.number_input("Temps minimum par session", value=60, step=30)
            with c2: 
                break_min_heavy = st.number_input("Pause min après grosse session", value=60, step=15)
                
            break_max_cond = 30
            creneau_min_adj = 30
            
            st.markdown("---")
            st.markdown("#### 🌙 Nuit")
            st.write("Sélectionnez une sous-plage horaire de votre nuit")
            
            # Génération des 24 intervalles heure par heure avec un pas de 5h
            options_intervalle = [f"{h:02d}:00 - {(h+5)%24:02d}:00" for h in range(24)]
            intervalle_nuit = st.selectbox("Nuit intervalle (5h)", options_intervalle, index=options_intervalle.index("00:00 - 05:00"))

            st.markdown("---")
            
            st.subheader("Ajouter une disponibilité")
            jours_choisis = st.multiselect("Jours concernés", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
            
            liste_heures = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)] + ["23:59"]
            
            col1, col2 = st.columns(2)
            with col1: 
                debut_str = st.selectbox("Arrivée", liste_heures, index=liste_heures.index("18:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
            with col2: 
                fin_str = st.selectbox("Départ", liste_heures, index=liste_heures.index("22:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
                
            if st.form_submit_button("Enregistrer pour ces jours"):
                if not jours_choisis:
                    st.error("Veuillez sélectionner au moins un jour.")
                else:
                    for j in jours_choisis:
                        nouvelle_dispo = {
                            "id": str(uuid.uuid4()),
                            "nom": nom_joueur.strip(), "jour": j,
                            "debut": debut_str, "fin": fin_str,
                            "limite_max": limite_max,
                            "t_max_affile": temps_max_affile, "t_min_base": creneau_min_base,
                            "break_min_heavy": break_min_heavy, "break_max_cond": break_max_cond,
                            "t_min_adj": creneau_min_adj,
                            "heures_max_hebdo": heures_max_hebdo,
                            "heures_max_jour": heures_max_jour,
                            "intervalle_nuit": intervalle_nuit
                        }
                        ajouter_dispo(nouvelle_dispo)
                        
                    st.success("Créneaux ajoutés avec succès !")
                    st.rerun()

        # --- NOUVEAU BOUTON : METTRE À JOUR LE PROFIL ---
        st.markdown("---")
        st.markdown("#### 🔄 Mettre à jour mon profil")
        st.write("Un changement de rythme ? Appliquez vos paramètres actuels (nuit, max heures...) à tous vos créneaux existants d'un coup.")
        
        if st.button("Mettre à jour tous mes anciens créneaux", type="secondary"):
            donnees_actuelles = charger_donnees()
            creneaux_joueur = [d for d in donnees_actuelles if d["nom"] == nom_joueur.strip()]
            
            if not creneaux_joueur:
                st.warning("Vous n'avez aucun créneau enregistré à mettre à jour.")
            else:
                with st.spinner("Mise à jour en cours..."):
                    for d in creneaux_joueur:
                        d["limite_max"] = limite_max
                        d["heures_max_hebdo"] = heures_max_hebdo
                        d["heures_max_jour"] = heures_max_jour
                        d["t_max_affile"] = temps_max_affile
                        d["t_min_base"] = creneau_min_base
                        d["break_min_heavy"] = break_min_heavy
                        d["intervalle_nuit"] = intervalle_nuit
                        ajouter_dispo(d)
                st.success("✅ Vos anciens créneaux intègrent désormais votre nouvelle logique de nuit !")
                st.rerun()

        st.markdown("---")
        st.subheader("Vos créneaux enregistrés")
        donnees = charger_donnees()
        mes_dispos = [d for d in donnees if d["nom"] == nom_joueur.strip()]
        
        if mes_dispos:
            for d in mes_dispos:
                col_info, col_btn = st.columns([4, 1])
                with col_info:
                    fin_affichee = "Minuit" if d['fin'] == "23:59" else d['fin']
                    st.write(f"**{d['jour']}** : {d['debut']} - {fin_affichee} *(Limite: {d.get('limite_max', 'Non définie')})*")
                with col_btn:
                    if st.button("❌", key=d["id"]):
                        supprimer_entree(d["id"])
                        st.rerun()

# --- VUE 2 : L'ADMINISTRATEUR ---
if est_admin:
    st.success("Mode Administrateur activé")
    donnees = charger_donnees()
    noms_dispos = list(set([d["nom"] for d in donnees])) if donnees else []
    
    if st.button("🗑️ Effacer la base de données"):
        vider_toutes_les_dispos()
        st.session_state.assignations_forcees = []
        sauvegarder_matrice_affinites({
            "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
            "100": {"Planning 250": 1, "Planning 100": 3, "Planning 50": 3},
            "50": {"Planning 250": 1, "Planning 100": 1, "Planning 50": 3},
            "repos_nuit_min": 10
        })
        st.rerun()

    tab_liste, tab_force, tab_param, tab_gen = st.tabs(["📋 Liste des Créneaux", "🛠️ Assignations", "⚙️ Paramètres", "🚀 Génération & Planning"])

    with tab_liste:
        if donnees:
            df = pd.DataFrame(donnees)
            st.dataframe(df.drop(columns=["id"]), use_container_width=True)
            
            options_suppression = {f"{d['nom']} | {d['jour']} {d['debut']}-{d['fin']}": d['id'] for d in donnees}
            creneaux_a_supprimer = st.multiselect("Supprimer des créneaux :", list(options_suppression.keys()))
            if st.button("🗑️ Supprimer la sélection", type="primary") and creneaux_a_supprimer:
                for sel in creneaux_a_supprimer: 
                    supprimer_entree(options_suppression[sel])
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
        st.subheader("Matrice des Affinités")
        st.write("Définissez vers quels plannings orienter l'algorithme en fonction de la 'Limite Max' choisie par le joueur. *(1 = On évite, 5 = On priorise)*")
        
        affinites_admin = charger_matrice_affinites()
        
        with st.form("form_affinites"):
            st.markdown("**Pour les joueurs avec Limite Max = 250 :**")
            col_a1, col_b1, col_c1 = st.columns(3)
            with col_a1: val_250_250 = st.slider("Planning 250", 1, 5, affinites_admin.get("250", {}).get("Planning 250", 3), key="s_250_250")
            with col_b1: val_100_250 = st.slider("Planning 100", 1, 5, affinites_admin.get("250", {}).get("Planning 100", 3), key="s_100_250")
            with col_c1: val_50_250 = st.slider("Planning 50", 1, 5, affinites_admin.get("250", {}).get("Planning 50", 3), key="s_50_250")
            affinites_admin["250"] = {"Planning 250": val_250_250, "Planning 100": val_100_250, "Planning 50": val_50_250}
            st.write("") 
            
            st.markdown("**Pour les joueurs avec Limite Max = 100 :**")
            col_a2, col_b2 = st.columns(2)
            with col_a2: val_100_100 = st.slider("Planning 100", 1, 5, affinites_admin.get("100", {}).get("Planning 100", 3), key="s_100_100")
            with col_b2: val_50_100 = st.slider("Planning 50", 1, 5, affinites_admin.get("100", {}).get("Planning 50", 3), key="s_50_100")
            affinites_admin["100"] = {"Planning 250": 1, "Planning 100": val_100_100, "Planning 50": val_50_100}
            st.write("")

            st.markdown("**Pour les joueurs avec Limite Max = 50 :**")
            val_50_50 = st.slider("Planning 50", 1, 5, affinites_admin.get("50", {}).get("Planning 50", 3), key="s_50_50")
            affinites_admin["50"] = {"Planning 250": 1, "Planning 100": 1, "Planning 50": val_50_50}
            st.write("")

            st.markdown("---")
            st.markdown("**🛌 Paramètres de Santé & Repos :**")
            repos_nuit = st.number_input("Durée de la nuit obligatoire (Heures consécutives)", value=affinites_admin.get("repos_nuit_min", 10), step=1)
            affinites_admin["repos_nuit_min"] = repos_nuit
            st.write("")
                
            if st.form_submit_button("Enregistrer la matrice", type="primary"):
                sauvegarder_matrice_affinites(affinites_admin)
                st.success("Matrice enregistrée et appliquée pour les prochaines générations !")

    with tab_gen:
        st.subheader("Lancer l'Optimisation Mathématique")
        c1, c2 = st.columns(2)
        with c1: resolution = st.number_input("Résolution (min)", value=30, step=5)
        with c2: joueurs_simultanes = st.number_input("Joueurs max par planning", value=1, min_value=1)

        if st.button("🚀 Résoudre la semaine entière", type="primary"):
            if not donnees:
                st.error("Aucune donnée.")
            else:
                matrice = charger_matrice_affinites()
                
                with st.spinner('Le solveur calcule la répartition optimale sur toute la semaine...'):
                    planning_complet, temps_totaux = optimiser_planning_hebdo(
                        donnees, resolution, joueurs_simultanes, 
                        st.session_state.assignations_forcees, matrice
                    )
                    
                    st.session_state.planning_complet = planning_complet
                
                if not planning_complet:
                    st.warning("Aucun créneau n'a pu être généré. Vérifiez les contraintes et les disponibilités.")
                else:
                    st.success("Planning optimal trouvé pour la semaine !")
                    
                    st.markdown("### 📅 Emploi du temps")
                    st.markdown(generer_grille_html(planning_complet, noms_dispos, resolution), unsafe_allow_html=True)
                    
                    st.markdown("### 📊 Temps de jeu total (Hebdomadaire)")
                    stats_data = [{"Joueur": j, "Temps (Heures)": t/60.0} for j, t in temps_totaux.items() if t > 0]
                    if stats_data:
                        df_stats = pd.DataFrame(stats_data)
                        barres = alt.Chart(df_stats).mark_bar().encode(
                            x=alt.X('Joueur:O', sort='-y'),
                            y='Temps (Heures):Q'
                        )
                        st.altair_chart(barres, use_container_width=True)

        if 'planning_complet' in st.session_state and st.session_state.planning_complet:
            st.markdown("---")
            st.markdown("### ☁️ Publication en ligne")
            
            if st.button("🌐 Mettre à jour le Google Sheet en direct", type="primary"):
                with st.spinner("Envoi des données vers Google Sheets..."):
                    try:
                        mettre_a_jour_google_sheet(st.session_state.planning_complet, resolution)
                        st.success("✅ Le Google Sheet a été mis à jour avec succès ! Les joueurs peuvent rafraîchir leur page.")
                        st.markdown("[Lien vers le Google Sheet public](https://docs.google.com/spreadsheets/d/1wl_RLPs1h7TsUQFDQj6An0ouhU1-KlXEmBURksGDPic/edit)")
                    except Exception as e:
                        st.error(f"Erreur lors de la mise à jour : {e}")
