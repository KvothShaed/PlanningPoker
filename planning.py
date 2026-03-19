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
    h_max = max([datetime.strptime(d["fin"], "%H:%M") for d in donnees_jour])
    
    creneaux = []
    actuel = h_min
    while actuel < h_max:
        creneaux.append(actuel.strftime('%H:%M'))
        actuel += timedelta(minutes=resolution)

    joueurs_data = {d["nom"]: d for d in donnees_jour}
    joueurs = list(joueurs_data.keys())
    
    X = pulp.LpVariable.dicts("Assign", ((j, p, c) for j in joueurs for p in PLANNINGS_DISPOS for c in creneaux), cat='Binary')
    Y = pulp.LpVariable.dicts("Joue", ((j, c) for j in joueurs for c in creneaux), cat='Binary')

    for c in creneaux:
        for j in joueurs:
            prob += Y[j, c] == pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS), f"Lien_XY_{j}_{c}"
            prob += pulp.lpSum(X[j, p, c] for p in PLANNINGS_DISPOS) <= 1, f"Unicite_{j}_{c}"

    objectif = []
    for j, d in joueurs_data.items():
        limite_joueur = str(d.get("limite_max", 250))
        prefs_admin = matrice_affinites.get(limite_joueur, {"Planning 250": 1, "Planning 100": 1, "Planning 50": 1})
        
        for c in creneaux:
            if d["debut"] <= c < d["fin"]:
                for p in PLANNINGS_DISPOS:
                    # On sécurise avec un .get() au cas où la matrice du JSON n'est pas à jour
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
def generer_grille_html(planning, joueurs_uniques):
    if not planning: return "<p>Aucun planning généré.</p>"
    couleurs = ["#FFADAD", "#FFD6A5", "#FDFFB6", "#CAFFBF", "#9BF6FF", "#A0C4FF", "#BDB2FF", "#FFC6FF", "#FFFFFC"]
    map_couleurs = {j: couleurs[i % len(couleurs)] for i, j in enumerate(joueurs_uniques)}
    
    jours = sorted(list(set([p["Jour"] for p in planning])))
    horaires = sorted(list(set([p["Horaire"] for p in planning])))
    
    html = "<table style='width:100%; border-collapse: collapse; text-align: center; font-family: sans-serif;'>"
    html += "<tr><th style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>Horaire</th>"
    
    colonnes = []
    for j in jours:
        for p in PLANNINGS_DISPOS:
            colonnes.append((j, p))
            html += f"<th style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>{j}<br><small>{p}</small></th>"
    html += "</tr>"
    
    for h in horaires:
        html += f"<tr><td style='border: 1px solid #ddd; padding: 8px; font-weight: bold; white-space: nowrap;'>{h}</td>"
        for j, p in colonnes:
            slot = next((item for item in planning if item["Jour"] == j and item["Horaire"] == h and item["Planning"] == p), None)
            html += "<td style='border: 1px solid #ddd; padding: 4px;'>"
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
            jours_choisis = st.multiselect("Jours concernés", ["Lundi", "Mardi", "Mercredi", "
