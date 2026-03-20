import streamlit as st
import pandas as pd
import json
import os
import uuid
import pulp
import altair as alt
from datetime import datetime, timedelta

st.set_page_config(page_title="Planning Optimisé & Multisites", layout="wide")

FICHIER_DONNEES = "dispos_avancees.json"
FICHIER_AFFINITES = "config_affinites.json"
PLANNINGS_DISPOS = ["Planning 250", "Planning 100", "Planning 50"]

if 'assignations_forcees' not in st.session_state:
    st.session_state.assignations_forcees = []

# --- GESTION DES DONNÉES ---
def charger_donnees():
    if os.path.exists(FICHIER_DONNEES):
        with open(FICHIER_DONNEES, "r") as f: return json.load(f)
    return []

def sauvegarder_donnees(data):
    with open(FICHIER_DONNEES, "w") as f: json.dump(data, f)

def supprimer_entree(id_entree):
    donnees = charger_donnees()
    donnees = [d for d in donnees if d.get("id") != id_entree]
    sauvegarder_donnees(donnees)

def charger_matrice_affinites():
    if os.path.exists(FICHIER_AFFINITES):
        with open(FICHIER_AFFINITES, "r") as f: return json.load(f)
    return {
        "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
        "100": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
        "50": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3}
    }

def sauvegarder_matrice_affinites(data):
    with open(FICHIER_AFFINITES, "w") as f: json.dump(data, f)

# --- MOTEUR D'OPTIMISATION MATHÉMATIQUE (PuLP) ---
def optimiser_planning_pulp(donnees_jour, resolution, joueurs_simultanes, assignations_forcees, jour_cible, matrice_affinites):
    if not donnees_jour:
        return [], {}

    prob = pulp.LpProblem(f"Optimisation_{jour_cible}", pulp.LpMaximize)
    
    h_min = min([datetime.strptime(d["debut"], "%H:%M") for d in donnees_jour])
    h_max = max([datetime.strptime(d["fin"], "%H:%M") for d in donnees_jour if d["fin"] != "23:59"] + [datetime.strptime("23:59", "%H:%M")])
    
    creneaux = []
    actuel = h_min
    while actuel < h_max:
        creneaux.append(actuel.strftime('%H:%M'))
        actuel += timedelta(minutes=resolution)

    joueurs_data = {d["nom"]: d for d in donnees_jour}
    joueurs = list(joueurs_data.keys())
    
    X = pulp.LpVariable.dicts("Assign", ((j, p, c) for j in joueurs for p in PLANNINGS_DISPOS for c in creneaux), cat='Binary')
    Y = pulp.LpVariable.dicts("Joue", ((j, c) for j in joueurs for c in creneaux), cat='Binary')

    # NOUVELLES VARIABLES POUR L'ÉQUITÉ
    Temps_Total = pulp.LpVariable.dicts("TempsTotal", joueurs, lowBound=0, cat='Integer')
    Max_Temps = pulp.LpVariable("MaxTemps", lowBound=0, cat='Integer')
    Min_Temps = pulp.LpVariable("MinTemps", lowBound=0, cat='Integer')

    plannings_autorises = {
        "250": ["Planning 250", "Planning 100", "Planning 50"],
        "100": ["Planning 100", "Planning 50"],
        "50":  ["Planning 50"]
    }

    # Calcul des temps totaux et définition du Max/Min
    for j in joueurs:
        prob += Temps_Total[j] == pulp.lpSum(Y[j, c] for c in creneaux), f"Calc_Temps_{j}"
        prob += Max_Temps >= Temps_Total[j], f"Def_Max_{j}"
        prob += Min_Temps <= Temps_Total[j], f"Def_Min_{j}"

    for c in creneaux:
        for j in joueurs:
            prob += Y[j, c] == pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS), f"Lien_XY_{j}_{c}"
            prob += pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS) <= 1, f"Unicite_{j}_{c}"

    # FONCTION OBJECTIF MULTI-CRITÈRES
    objectif = []
    
    # 1. Remplir le plus possible (+1000 par slot joué)
    objectif.append(1000 * pulp.lpSum(Y[j, c] for j in joueurs for c in creneaux))
    
    # 2. Équité : Pénaliser l'écart entre le plus gros et le plus petit temps de jeu (-100 par slot d'écart)
    objectif.append(-100 * (Max_Temps - Min_Temps))

    # 3. Affinités (+1 à 5 par slot sur le bon planning)
    for j, d in joueurs_data.items():
        limite_joueur = str(d.get("limite_max", 250))
        prefs_admin = matrice_affinites.get(limite_joueur, {"Planning 250": 1, "Planning 100": 1, "Planning 50": 1})
        autorises = plannings_autorises.get(limite_joueur, PLANNINGS_DISPOS)
        
        for c in creneaux:
            if d["debut"] <= c < (d["fin"] if d["fin"] != "23:59" else "24:00"):
                for p in PLANNINGS_DISPOS:
                    if p not in autorises:
                        prob += X[j, p, c] == 0, f"Interdit_{j}_{p}_{c}"
                    else:
                        valeur_pref = prefs_admin.get(p, 1)
                        objectif.append(valeur_pref * X[j, p, c])
            else:
                prob += Y[j, c] == 0, f"Indispo_{j}_{c}"
                
    prob += pulp.lpSum(objectif)

    for c in creneaux:
        for p in PLANNINGS_DISPOS:
            prob += pulp.lpSum(X[j, p, c] for j in joueurs) <= joueurs_simultanes, f"Cap_{p}_{c}"

    for j, d in joueurs_data.items():
        max_slots = int(d["t_max_affile"] / resolution)
        min_slots = int(d["t_min_base"] / resolution)
        
        if max_slots > 0 and len(creneaux) > max_slots:
            for i in range(len(creneaux) - max_slots):
                prob += pulp.lpSum(Y[j, creneaux[i+k]] for k in range(max_slots + 1)) <= max_slots, f"Max_Affile_{j}_{i}"
                
        if min_slots > 1:
            for i in range(1, len(creneaux) - min_slots + 1):
                start_var = Y[j, creneaux[i]] - Y[j, creneaux[i-1]]
                for k in range(1, min_slots):
                    prob += start_var <= Y[j, creneaux[i+k]], f"Min_Base_{j}_{i}_{k}"

    for force in assignations_forcees:
        if force['jour'] == jour_cible and force['nom'] in joueurs and force['heure'] in creneaux:
            p_force = force['planning']
            prob += X[force['nom'], p_force, force['heure']] == 1, f"Force_{force['nom']}_{force['heure']}"

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    
    planning_jour = []
    temps_global = {j: 0 for j in joueurs}
    
    if pulp.LpStatus[prob.status] == 'Optimal':
        for c in creneaux:
            for p in PLANNINGS_DISPOS:
                joueurs_assignes = [j for j in joueurs if pulp.value(X[j, p, c]) == 1]
                if joueurs_assignes:
                    planning_jour.append({
                        "Jour": jour_cible,
                        "Horaire": c,
                        "Planning": p,
                        "Joueurs_Liste": joueurs_assignes
                    })
                    for j in joueurs_assignes:
                        temps_global[j] += resolution

    return planning_jour, temps_global

# --- GÉNÉRATEUR VISUEL ---
def generer_grille_html(planning, joueurs_uniques, resolution):
    if not planning: return "<p>Aucun planning généré.</p>"
    couleurs = ["#FFADAD", "#FFD6A5", "#FDFFB6", "#CAFFBF", "#9BF6FF", "#A0C4FF", "#BDB2FF", "#FFC6FF", "#FFFFFC"]
    map_couleurs = {j: couleurs[i % len(couleurs)] for i, j in enumerate(joueurs_uniques)}
    
    ordre_jours = {"Lundi":0, "Mardi":1, "Mercredi":2, "Jeudi":3, "Vendredi":4, "Samedi":5, "Dimanche":6}
    jours = sorted(list(set([p["Jour"] for p in planning])), key=lambda j: ordre_jours.get(j, 7))
    horaires_str = sorted(list(set([p["Horaire"] for p in planning])))
    
    couleurs_colonnes = {"Planning 250": "#ffebee", "Planning 100": "#e8f5e9", "Planning 50": "#e3f2fd"}
    couleurs_cellules = {"Planning 250": "#fffafb", "Planning 100": "#fafffa", "Planning 50": "#fbfdff"}
    
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
                bg_color = couleurs_cellules[p]
                html += f"<td style='border: 1px solid #ddd; padding: 4px; background-color: {bg_color};'>"
                if slot and slot["Joueurs_Liste"]:
                    for joueur in slot["Joueurs_Liste"]:
                        c = map_couleurs.get(joueur, "#eee")
                        html += f"<div style='background-color: {c}; color: #000; margin: 2px; padding: 4px; border-radius: 6px; font-size: 0.85em; font-weight: 500;'>{joueur}</div>"
                html += "</td>"
        html += "</tr>"
    html += "</table>"
    return html

# ==========================================
# INTERFACE UTILISATEUR
# ==========================================
st.title("Générateur de Planning Multi-Sites Optimisé 🗓️")

with st.sidebar:
    st.header("👑 Accès Admin")
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "Romarino7") 

# --- VUE 1 : LES JOUEURS ---
if not est_admin:
    st.header("👤 Espace Joueur")
    nom_joueur = st.text_input("Identifiez-vous (Votre Prénom / Nom) :")
    
    if nom_joueur.strip():
        st.markdown(f"### Bienvenue {nom_joueur} !")
        
        with st.form("formulaire_dispo", clear_on_submit=False):
            st.subheader("Ajouter une disponibilité")
            jours_choisis = st.multiselect("Jours concernés", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
            
            liste_heures = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)] + ["23:59"]
            
            col1, col2 = st.columns(2)
            with col1: 
                debut_str = st.selectbox("Arrivée", liste_heures, index=liste_heures.index("18:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
            with col2: 
                fin_str = st.selectbox("Départ", liste_heures, index=liste_heures.index("22:00"), format_func=lambda x: "Minuit" if x == "23:59" else x)
            
            st.markdown("#### 🎯 Paramètre Joueur")
            limite_max = st.selectbox("Sélectionnez votre Limite Max", [250, 100, 50])
                
            st.markdown("#### ⚙️ Contraintes de rythme")
            c1, c2 = st.columns(2)
            with c1:
                temps_max_affile = st.number_input("Max temps d'affilée (min)", value=120, step=30)
                creneau_min_base = st.number_input("Temps minimum par session", value=60, step=30)
            with c2: 
                break_min_heavy = st.number_input("Pause min après grosse session", value=60, step=15)
                
            c3, c4 = st.columns(2)
            with c3: break_max_cond = st.number_input("Si pause moins de (min)", value=30, step=15)
            with c4: creneau_min_adj = st.number_input("...jouer au moins", value=30, step=15)
            
            if st.form_submit_button("Enregistrer pour ces jours"):
                if not jours_choisis:
                    st.error("Veuillez sélectionner au moins un jour.")
                else:
                    donnees = charger_donnees()
                    for j in jours_choisis:
                        donnees.append({
                            "id": str(uuid.uuid4()),
                            "nom": nom_joueur.strip(), "jour": j,
                            "debut": debut_str, "fin": fin_str,
                            "limite_max": limite_max,
                            "t_max_affile": temps_max_affile, "t_min_base": creneau_min_base,
                            "break_min_heavy": break_min_heavy, "break_max_cond": break_max_cond,
                            "t_min_adj": creneau_min_adj
                        })
                    sauvegarder_donnees(donnees)
                    st.success("Créneaux ajoutés avec succès !")
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
        sauvegarder_donnees([])
        st.session_state.assignations_forcees = []
        sauvegarder_matrice_affinites({
            "250": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
            "100": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3},
            "50": {"Planning 250": 3, "Planning 100": 3, "Planning 50": 3}
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
                
            if st.form_submit_button("Enregistrer la matrice", type="primary"):
                sauvegarder_matrice_affinites(affinites_admin)
                st.success("Matrice enregistrée et appliquée pour les prochaines générations !")

    with tab_gen:
        st.subheader("Lancer l'Optimisation Mathématique")
        c1, c2 = st.columns(2)
        with c1: resolution = st.number_input("Résolution (min)", value=30, step=5)
        with c2: joueurs_simultanes = st.number_input("Joueurs max par planning", value=1, min_value=1)

        if st.button("🚀 Résoudre le planning", type="primary"):
            if not donnees:
                st.error("Aucune donnée.")
            else:
                planning_complet = []
                temps_totaux = {j: 0 for j in noms_dispos}
                jours_presents = list(set([d["jour"] for d in donnees]))
                matrice = charger_matrice_affinites()
                
                with st.spinner('Le solveur calcule la répartition optimale...'):
                    for jour in jours_presents:
                        donnees_jour = [d for d in donnees if d["jour"] == jour]
                        planning_j, temps_j = optimiser_planning_pulp(
                            donnees_jour, resolution, joueurs_simultanes, 
                            st.session_state.assignations_forcees, jour, matrice
                        )
                        planning_complet.extend(planning_j)
                        for j, t in temps_j.items():
                            temps_totaux[j] += t
                
                st.success("Planning optimal trouvé !")
                
                st.markdown("### 📅 Emploi du temps")
                st.markdown(generer_grille_html(planning_complet, noms_dispos, resolution), unsafe_allow_html=True)
                
                st.markdown("### 📊 Temps de jeu total")
                stats_data = [{"Joueur": j, "Temps (Heures)": t/60.0} for j, t in temps_totaux.items() if t > 0]
                if stats_data:
                    df_stats = pd.DataFrame(stats_data)
                    barres = alt.Chart(df_stats).mark_bar().encode(
                        x=alt.X('Joueur:O', sort='-y'),
                        y='Temps (Heures):Q'
                    )
                    st.altair_chart(barres, use_container_width=True)
