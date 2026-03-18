import streamlit as st
import pandas as pd
from datetime import datetime, timedelta

st.set_page_config(page_title="Planning Uniforme", layout="centered")

st.title("Générateur de Planning Partagé 🗓️")
st.markdown("Entrez les disponibilités des joueurs. L'algorithme répartit le temps de jeu de manière équitable.")

# --- PARAMÈTRES ---
st.header("1. Paramètres de la session")
col1, col2, col3 = st.columns(3)
with col1:
    plage_debut = st.time_input("Début", value=pd.to_datetime("18:00").time())
with col2:
    plage_fin = st.time_input("Fin", value=pd.to_datetime("22:00").time())
with col3:
    duree_creneau = st.number_input("Durée (min)", value=30, step=10)

joueurs_par_creneau = st.number_input("Nombre de joueurs par créneau (ex: 2 pour un match, 4 pour un double)", value=2, min_value=1)

# --- DONNÉES JOUEURS ---
st.header("2. Disponibilités")
exemple_data = "Alice, 18:00, 20:00\nBob, 18:30, 22:00\nCharlie, 18:00, 22:00\nDavid, 19:00, 21:00\nEmma, 18:00, 22:00"
input_text = st.text_area("Format : Nom, Heure d'arrivée, Heure de départ (Un par ligne)", value=exemple_data, height=150)

# --- ALGORITHME DE RÉPARTITION ---
def generer_planning(debut, fin, duree, texte_joueurs, max_joueurs):
    creneaux = []
    actuel = datetime.combine(datetime.today(), debut)
    fin_dt = datetime.combine(datetime.today(), fin)
    
    # 1. Génération des créneaux
    while actuel + timedelta(minutes=duree) <= fin_dt:
        creneaux.append({
            "debut": actuel.time(),
            "fin": (actuel + timedelta(minutes=duree)).time(),
            "joueurs_assignes": []
        })
        actuel += timedelta(minutes=duree)

    # 2. Lecture des joueurs
    joueurs = []
    for ligne in texte_joueurs.strip().split('\n'):
        if ligne:
            parts = [p.strip() for p in ligne.split(',')]
            if len(parts) == 3:
                joueurs.append({
                    "nom": parts[0], 
                    "debut": pd.to_datetime(parts[1]).time(), 
                    "fin": pd.to_datetime(parts[2]).time(), 
                    "compteur": 0 # Suit le nombre de fois où le joueur a joué
                })

    # 3. Assignation uniforme
    for c in creneaux:
        dispos = []
        for j in joueurs:
            # Vérifier si le joueur est dispo sur la totalité du créneau
            if j["debut"] <= c["debut"] and j["fin"] >= c["fin"]:
                dispos.append(j)
        
        # Le secret de l'uniformité : on trie par ceux qui ont le plus petit compteur
        dispos.sort(key=lambda x: x["compteur"])
        
        # On assigne les N premiers joueurs dispo (ceux qui ont le moins joué)
        assignes = dispos[:max_joueurs] 
        for a in assignes:
            a["compteur"] += 1
            c["joueurs_assignes"].append(a["nom"])

    return creneaux, joueurs

# --- RÉSULTATS ---
if st.button("Générer le planning", type="primary"):
    creneaux, stats_joueurs = generer_planning(plage_debut, plage_fin, duree_creneau, input_text, joueurs_par_creneau)
    
    st.header("Planning Généré")
    df_planning = pd.DataFrame([
        {
            "Horaire": f"{c['debut'].strftime('%H:%M')} - {c['fin'].strftime('%H:%M')}", 
            "Joueurs": " | ".join(c['joueurs_assignes']) if c['joueurs_assignes'] else "Pas assez de joueurs dispos"
        } 
        for c in creneaux
    ])
    st.table(df_planning)

    st.header("Équilibre du temps de jeu")
    df_stats = pd.DataFrame([{"Joueur": j["nom"], "Créneaux joués": j["compteur"]} for j in stats_joueurs])
    if not df_stats.empty:
        st.bar_chart(df_stats.set_index("Joueur"))