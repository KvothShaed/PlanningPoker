import streamlit as st
import pandas as pd
import json
import os
import random
import uuid
from datetime import datetime, timedelta

st.set_page_config(page_title="Planning Optimisé", layout="wide")

FICHIER_DONNEES = "dispos_avancees.json"

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

# --- MOTEUR D'OPTIMISATION (Identique) ---
def calculer_score(planning_final, temps_global):
    places_remplies = sum(len(p["Joueurs_Liste"]) for p in planning_final)
    score = places_remplies * 1000 
    temps_joues = list(temps_global.values())
    if temps_joues:
        score -= (max(temps_joues) - min(temps_joues))
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
            
            # Injections forcées
            for force in assignations_forcees:
                if force['jour'] == jour and force['heure'] == heure_str:
                    nom_force = force['nom']
                    if nom_force in etat_joueurs and nom_force not in joueurs_sur_terrain:
                        etat = etat_joueurs[nom_force]
                        c_rules = etat["contraintes"]
                        duree_bloc = c_rules["t_min_base"] 
                        joueurs_sur_terrain.append(nom_force)
                        etat["en_jeu_jusqua"] = actuel + timedelta(minutes=duree_bloc)
                        etat["temps_consecutif"] += duree_bloc
                        etat["derniere_fin_session"] = etat["en_jeu_jusqua"]
                        temps_global[nom_force] += duree_bloc
            
            places_restantes = joueurs_simultanes - len(joueurs_sur_terrain)
            
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
                    
                    if etat["temps_consecutif"] >= (c_rules["t_max_affile"] * 0.75):
                        etat["en_pause_jusqua"] = etat["en_jeu_jusqua"] + timedelta(minutes=c_rules["break_min_heavy"])
                        etat["temps_consecutif"] = 0 
            
            planning_essai.append({
                "Jour": jour, "Horaire": heure_str,
                "Joueurs_Liste": joueurs_sur_terrain.copy()
            })
            
            actuel += timedelta(minutes=resolution)
            for nom, etat in etat_joueurs.items():
                if nom not in joueurs_sur_terrain and etat["temps_consecutif"] > 0 and actuel >= etat["en_pause_jusqua"]:
                     if (actuel - etat["derniere_fin_session"]).total_seconds() / 60 >= 30:
                         etat["temps_consecutif"] = 0

    return planning_essai, temps_global

# --- GÉNÉRATEUR VISUEL (Tableau HTML avec Couleurs) ---
def generer_grille_html(planning, joueurs_uniques):
    couleurs = ["#FFADAD", "#FFD6A5", "#FDFFB6", "#CAFFBF", "#9BF6FF", "#A0C4FF", "#BDB2FF", "#FFC6FF", "#FFFFFC"]
    map_couleurs = {j: couleurs[i % len(couleurs)] for i, j in enumerate(joueurs_uniques)}
    
    jours = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    horaires = sorted(list(set([p["Horaire"] for p in planning])))
    
    html = "<table style='width:100%; border-collapse: collapse; text-align: center; font-family: sans-serif;'>"
    html += "<tr><th style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>Horaire</th>"
    for j in jours: html += f"<th style='border: 1px solid #ddd; padding: 10px; background-color: #f4f4f4;'>{j}</th>"
    html += "</tr>"
    
    for h in horaires:
        html += f"<tr><td style='border: 1px solid #ddd; padding: 8px; font-weight: bold;'>{h}</td>"
        for j in jours:
            slot = next((p for p in planning if p["Jour"] == j and p["Horaire"] == h), None)
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
st.title("Générateur de Planning Optimisé 🗓️")

with st.sidebar:
    st.header("👑 Accès Admin")
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "admin123") 

# --- VUE 1 : LES JOUEURS ---
if not est_admin:
    st.header("👤 Espace Joueur")
    nom_joueur = st.text_input("Identifiez-vous (Votre Prénom / Nom) :")
    
    if nom_joueur.strip():
        st.markdown(f"### Bienvenue {nom_joueur} !")
        
        # 1. FORMULAIRE D'AJOUT (Avec sélection multiple des jours)
        with st.form("formulaire_dispo", clear_on_submit=False):
            st.subheader("Ajouter une disponibilité")
            jours_choisis = st.multiselect("Jours concernés (Vous pouvez en sélectionner plusieurs)", 
                                           ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
            
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
            
            if st.form_submit_button("Enregistrer pour ces jours"):
                if not jours_choisis:
                    st.error("Veuillez sélectionner au moins un jour.")
                else:
                    donnees = charger_donnees()
                    for j in jours_choisis:
                        donnees.append({
                            "id": str(uuid.uuid4()), # ID Unique pour la suppression
                            "nom": nom_joueur.strip(), "jour": j,
                            "debut": debut.strftime("%H:%M"), "fin": fin.strftime("%H:%M"),
                            "t_max_affile": temps_max_affile, "t_min_base": creneau_min_base,
                            "break_min_heavy": break_min_heavy, "break_max_cond": break_max_cond,
                            "t_min_adj": creneau_min_adj
                        })
                    sauvegarder_donnees(donnees)
                    st.success(f"Créneaux ajoutés pour : {', '.join(jours_choisis)} !")
                    st.rerun()

        # 2. GESTION DES CRÉNEAUX EXISTANTS
        st.markdown("---")
        st.subheader("Vos créneaux enregistrés")
        donnees = charger_donnees()
        mes_dispos = [d for d in donnees if d["nom"] == nom_joueur.strip()]
        
        if not mes_dispos:
            st.info("Vous n'avez pas encore renseigné de disponibilités.")
        else:
            for d in mes_dispos:
                col_info, col_btn = st.columns([4, 1])
                with col_info:
                    st.write(f"**{d['jour']}** : {d['debut']} - {d['fin']} *(Max: {d['t_max_affile']}min, Min: {d['t_min_base']}min)*")
                with col_btn:
                    if st.button("❌ Supprimer", key=d["id"]):
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
        st.rerun()

    # Utilisation des onglets pour organiser la vue Admin
    tab_liste, tab_force, tab_gen = st.tabs(["📋 Liste des Créneaux", "🛠️ Assignations", "🚀 Génération & Planning"])

    with tab_liste:
        st.subheader("Vue d'ensemble des disponibilités")
        if donnees:
            df = pd.DataFrame(donnees)
            # Réorganisation des colonnes pour la lisibilité
            df = df[['nom', 'jour', 'debut', 'fin', 't_max_affile', 't_min_base', 'break_min_heavy']]
            st.dataframe(df.sort_values(by=['jour', 'debut']), use_container_width=True)
        else:
            st.warning("Aucune donnée enregistrée.")

    with tab_force:
        st.subheader("Forcer des assignations manuelles")
        col_f1, col_f2, col_f3, col_f4 = st.columns([2, 2, 2, 1])
        with col_f1: f_jour = st.selectbox("Jour", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"], key="f_jour")
        with col_f2: f_heure = st.time_input("Heure de début", value=pd.to_datetime("18:00").time(), key="f_heure")
        with col_f3: f_joueur = st.selectbox("Joueur", noms_dispos if noms_dispos else ["Aucun joueur"], key="f_joueur")
        with col_f4:
            st.write(""); st.write("")
            if st.button("➕ Ajouter"):
                if f_joueur != "Aucun joueur":
                    st.session_state.assignations_forcees.append({"jour": f_jour, "heure": f_heure.strftime("%H:%M"), "nom": f_joueur})
                    st.success(f"Ajouté : {f_joueur}")
        
        if st.session_state.assignations_forcees:
            st.write("**Règles manuelles actives :**")
            for regle in st.session_state.assignations_forcees:
                st.caption(f"- {regle['nom']} le {regle['jour']} à {regle['heure']}")
            if st.button("Effacer les règles manuelles"):
                st.session_state.assignations_forcees = []
                st.rerun()

    with tab_gen:
        st.subheader("Lancer l'Optimisation")
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
                    planning_test, temps_test = generer_un_planning_aleatoire(donnees, resolution, joueurs_simultanes, st.session_state.assignations_forcees)
                    score = calculer_score(planning_test, temps_test)
                    
                    if score > meilleur_score:
                        meilleur_score = score
                        meilleur_planning = planning_test
                        meilleur_temps = temps_test
                        
                    if i % 10 == 0:
                        barre_progression.progress(i / iterations)
                        texte_statut.write(f"Simulation en cours... (Score actuel : {meilleur_score})")
                
                barre_progression.progress(1.0)
                texte_statut.success("Optimisation terminée !")
                
                # AFFICHAGE DE LA GRILLE VISUELLE HTML
                st.markdown("### 📅 Emploi du temps de la semaine")
                grille_html = generer_grille_html(meilleur_planning, noms_dispos)
                st.markdown(grille_html, unsafe_allow_html=True)
                
                st.markdown("### 📊 Temps de jeu total")
                st.bar_chart(pd.DataFrame(list(meilleur_temps.items()), columns=["Joueur", "Temps (min)"]).set_index("Joueur"))
