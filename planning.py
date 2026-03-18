import streamlit as st
import pandas as pd
import json
import os
import random
from datetime import datetime, timedelta

st.set_page_config(page_title="Optimiseur de Planning", layout="wide")

FICHIER_DONNEES = "dispos_avancees.json"

# Initialisation de la mémoire pour les assignations forcées
if 'assignations_forcees' not in st.session_state:
    st.session_state.assignations_forcees = []

def charger_donnees():
    if os.path.exists(FICHIER_DONNEES):
        with open(FICHIER_DONNEES, "r") as f: return json.load(f)
    return []

def sauvegarder_donnees(data):
    with open(FICHIER_DONNEES, "w") as f: json.dump(data, f)

def calculer_score(planning_final, temps_global):
    places_remplies = sum(len(p["Joueurs_Liste"]) for p in planning_final)
    score = places_remplies * 1000 
    
    temps_joues = list(temps_global.values())
    if temps_joues:
        ecart = max(temps_joues) - min(temps_joues)
        score -= ecart 
        
    return score

def generer_un_planning_aleatoire(donnees, resolution, joueurs_simultanes, assignations_forcees):
    planning_essai = []
    temps_global = {d["nom"]: 0 for d in donnees}
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    
    for jour in jours:
        dispos_jour = [d for d in donnees if d["jour"] == jour]
        if not dispos_jour: continue
        
        h_min = min([datetime.strptime(d["debut"], "%H:%M") for d in dispos_jour])
        h_max = max([datetime.strptime(d["fin"], "%H:%M") for d in dispos_jour])
        
        etat_joueurs = {
            d["nom"]: {"en_jeu_jusqua": h_min, "en_pause_jusqua": h_min, "temps_consecutif": 0, "derniere_fin_session": h_min, "contraintes": d} 
            for d in dispos_jour
        }
        
        actuel = h_min
        while actuel < h_max:
            joueurs_sur_terrain = [nom for nom, etat in etat_joueurs.items() if etat["en_jeu_jusqua"] > actuel]
            heure_str = actuel.strftime('%H:%M')
            
            # --- NOUVEAUTÉ : INJECTION DES ASSIGNATIONS FORCÉES ---
            for force in assignations_forcees:
                if force['jour'] == jour and force['heure'] == heure_str:
                    nom_force = force['nom']
                    # Si le joueur est bien dans les données et pas déjà sur le terrain
                    if nom_force in etat_joueurs and nom_force not in joueurs_sur_terrain:
                        etat = etat_joueurs[nom_force]
                        c_rules = etat["contraintes"]
                        duree_bloc = c_rules["t_min_base"] # On lui réserve au moins son temps minimum
                        
                        joueurs_sur_terrain.append(nom_force)
                        etat["en_jeu_jusqua"] = actuel + timedelta(minutes=duree_bloc)
                        etat["temps_consecutif"] += duree_bloc
                        etat["derniere_fin_session"] = etat["en_jeu_jusqua"]
                        temps_global[nom_force] += duree_bloc
            
            places_restantes = joueurs_simultanes - len(joueurs_sur_terrain)
            
            # --- LE RESTE DE L'ALGO RESTE IDENTIQUE (Remplissage aléatoire intelligent) ---
            if places_restantes > 0:
                candidats_valides = []
                for d in dispos_jour:
                    nom = d["nom"]
                    etat = etat_joueurs[nom]
                    if nom in joueurs_sur_terrain: continue
                        
                    d_deb = datetime.strptime(d["debut"], "%H:%M")
                    d_fin = datetime.strptime(d["fin"], "%H:%M")
                    if d_deb > actuel or d_fin <= actuel: continue
                    if etat["en_pause_jusqua"] > actuel: continue
                        
                    pause_subie = (actuel - etat["derniere_fin_session"]).total_seconds() / 60
                    duree_bloc = d["t_min_base"]
                    if 0 < pause_subie <= d["break_max_cond"]:
                        duree_bloc = max(d["t_min_base"], d["t_min_adj"])
                        
                    if etat["temps_consecutif"] + duree_bloc > d["t_max_affile"]: continue
                    if actuel + timedelta(minutes=duree_bloc) > d_fin: continue 
                        
                    candidats_valides.append((nom, duree_bloc))
                
                random.shuffle(candidats_valides)
                candidats_valides.sort(key=lambda x: temps_global[x[0]] + random.randint(0, 30)) 
                
                for cand_nom, duree_bloc in candidats_valides[:places_restantes]:
                    joueurs_sur_terrain.append(cand_nom)
                    etat = etat_joueurs[cand_nom]
                    c_rules = etat["contraintes"]
                    
                    etat["en_jeu_jusqua"] = actuel + timedelta(minutes=duree_bloc)
                    etat["temps_consecutif"] += duree_bloc
                    etat["derniere_fin_session"] = etat["en_jeu_jusqua"]
                    temps_global[cand_nom] += duree_bloc
                    
                    seuil_heavy = c_rules["t_max_affile"] * 0.75
                    if etat["temps_consecutif"] >= seuil_heavy:
                        etat["en_pause_jusqua"] = etat["en_jeu_jusqua"] + timedelta(minutes=c_rules["break_min_heavy"])
                        etat["temps_consecutif"] = 0 
            
            planning_essai.append({
                "Jour": jour,
                "Horaire": f"{heure_str} - {(actuel + timedelta(minutes=resolution)).strftime('%H:%M')}",
                "Joueurs_Liste": joueurs_sur_terrain.copy(), 
                "Sur le terrain": " | ".join(joueurs_sur_terrain) if joueurs_sur_terrain else "Terrain Vide"
            })
            
            actuel += timedelta(minutes=resolution)
            for nom, etat in etat_joueurs.items():
                if nom not in joueurs_sur_terrain and etat["temps_consecutif"] > 0 and actuel >= etat["en_pause_jusqua"]:
                     if (actuel - etat["derniere_fin_session"]).total_seconds() / 60 >= 30:
                         etat["temps_consecutif"] = 0

    return planning_essai, temps_global

# ==========================================
# INTERFACE UTILISATEUR
# ==========================================
st.title("Générateur de Planning Optimisé 🗓️")

with st.sidebar:
    st.header("👑 Accès Admin")
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "admin123") 

if not est_admin:
    st.header("1. Vos disponibilités et contraintes")
    with st.form("formulaire_dispo", clear_on_submit=True):
        nom = st.text_input("Votre Prénom / Nom")
        jour = st.selectbox("Jour", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
        col1, col2 = st.columns(2)
        with col1: debut = st.time_input("Arrivée", value=pd.to_datetime("18:00").time())
        with col2: fin = st.time_input("Départ", value=pd.to_datetime("22:00").time())
            
        c1, c2 = st.columns(2)
        with c1:
            temps_max_affile = st.number_input("Max temps d'affilée (min)", value=120, step=30)
            creneau_min_base = st.number_input("Temps minimum par session", value=60, step=30)
        with c2: break_min_heavy = st.number_input("Pause min après grosse session", value=30, step=15)
            
        c3, c4 = st.columns(2)
        with c3: break_max_cond = st.number_input("Si pause moins de (min)", value=60, step=15)
        with c4: creneau_min_adj = st.number_input("...jouer au moins", value=60, step=15)
        
        if st.form_submit_button("Enregistrer"):
            if nom.strip():
                donnees = charger_donnees()
                donnees.append({
                    "nom": nom.strip(), "jour": jour,
                    "debut": debut.strftime("%H:%M"), "fin": fin.strftime("%H:%M"),
                    "t_max_affile": temps_max_affile, "t_min_base": creneau_min_base,
                    "break_min_heavy": break_min_heavy, "break_max_cond": break_max_cond,
                    "t_min_adj": creneau_min_adj
                })
                sauvegarder_donnees(donnees)
                st.success("Enregistré !")

if est_admin:
    st.success("Mode Administrateur")
    donnees = charger_donnees()
    noms_dispos = list(set([d["nom"] for d in donnees])) if donnees else []
    
    if st.button("🗑️ Effacer toutes les données"):
        sauvegarder_donnees([])
        st.session_state.assignations_forcees = []
        st.rerun()

    # --- ZONE D'ASSIGNATION MANUELLE ---
    st.markdown("---")
    st.subheader("🛠️ Forcer des assignations (Manuel)")
    st.write("Place manuellement des joueurs sur des créneaux précis avant de lancer l'optimisation.")
    
    col_f1, col_f2, col_f3, col_f4 = st.columns([2, 2, 2, 1])
    with col_f1: f_jour = st.selectbox("Jour", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"], key="f_jour")
    with col_f2: f_heure = st.time_input("Heure de début", value=pd.to_datetime("18:00").time(), key="f_heure")
    with col_f3: f_joueur = st.selectbox("Joueur", noms_dispos if noms_dispos else ["Aucun joueur"], key="f_joueur")
    with col_f4:
        st.write("") # Espacement
        st.write("")
        if st.button("➕ Ajouter"):
            if f_joueur != "Aucun joueur":
                st.session_state.assignations_forcees.append({
                    "jour": f_jour,
                    "heure": f_heure.strftime("%H:%M"),
                    "nom": f_joueur
                })
                st.success(f"{f_joueur} forcé à {f_heure.strftime('%H:%M')} le {f_jour}")
    
    # Affichage des règles forcées
    if st.session_state.assignations_forcees:
        st.write("**Règles manuelles actives :**")
        for i, regle in enumerate(st.session_state.assignations_forcees):
            st.caption(f"- {regle['nom']} le {regle['jour']} à {regle['heure']}")
        if st.button("Effacer les règles manuelles"):
            st.session_state.assignations_forcees = []
            st.rerun()
            
    st.markdown("---")
    # --- ZONE DE GÉNÉRATION ---
    st.subheader("⚙️ Lancer l'Optimisation")
    c1, c2, c3 = st.columns(3)
    with c1: resolution = st.number_input("Résolution (min)", value=15, step=5)
    with c2: joueurs_simultanes = st.number_input("Joueurs max", value=4, min_value=1)
    with c3: iterations = st.number_input("Simulations", value=500, step=100)

    if st.button("🚀 Chercher le meilleur planning", type="primary"):
        if not donnees:
            st.error("Aucune donnée.")
        else:
            meilleur_planning = None
            meilleur_score = -float('inf')
            meilleur_temps = None
            
            barre_progression = st.progress(0)
            texte_statut = st.empty()
            
            for i in range(iterations):
                # On passe les assignations forcées à la fonction
                planning_test, temps_test = generer_un_planning_aleatoire(donnees, resolution, joueurs_simultanes, st.session_state.assignations_forcees)
                score = calculer_score(planning_test, temps_test)
                
                if score > meilleur_score:
                    meilleur_score = score
                    meilleur_planning = planning_test
                    meilleur_temps = temps_test
                    
                if i % 10 == 0:
                    barre_progression.progress(i / iterations)
                    texte_statut.write(f"Simulation {i}/{iterations} | Score actuel : {meilleur_score}")
            
            barre_progression.progress(1.0)
            texte_statut.success("Terminé !")
            
            affichage = [{k: v for k, v in p.items() if k != 'Joueurs_Liste'} for p in meilleur_planning]
            st.dataframe(pd.DataFrame(affichage), use_container_width=True)
            st.bar_chart(pd.DataFrame(list(meilleur_temps.items()), columns=["Joueur", "Temps (min)"]).set_index("Joueur"))
