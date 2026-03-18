import streamlit as st
import pandas as pd
import json
import os
from datetime import datetime, timedelta

st.set_page_config(page_title="Planning Semaine", layout="centered")

# --- GESTION DES DONNÉES ---
FICHIER_DONNEES = "dispos.json"

def charger_donnees():
    """Charge les disponibilités depuis le fichier JSON."""
    if os.path.exists(FICHIER_DONNEES):
        with open(FICHIER_DONNEES, "r") as f:
            return json.load(f)
    return []

def sauvegarder_donnees(data):
    """Sauvegarde les disponibilités dans le fichier JSON."""
    with open(FICHIER_DONNEES, "w") as f:
        json.dump(data, f)

# --- INTERFACE PRINCIPALE ---
st.title("Générateur de Planning Hebdomadaire 🗓️")

# --- BARRE LATÉRALE : ACCÈS ADMIN ---
with st.sidebar:
    st.header("👑 Accès Administrateur")
    st.write("Réservé au créateur du planning.")
    # LE MOT DE PASSE EST ICI : change "admin123" par ce que tu veux
    mot_de_passe = st.text_input("Mot de passe", type="password")
    est_admin = (mot_de_passe == "admin123") 

# ==========================================
# VUE 1 : LES JOUEURS (Phase de collecte)
# ==========================================
if not est_admin:
    st.header("1. Indiquez vos disponibilités")
    st.info("Renseignez vos horaires pour la semaine à venir. Le planning sera généré une fois que tout le monde aura participé.")
    
    with st.form("formulaire_dispo", clear_on_submit=True):
        nom = st.text_input("Votre Prénom / Nom")
        jour = st.selectbox("Jour de la semaine", ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"])
        
        col1, col2 = st.columns(2)
        with col1:
            debut = st.time_input("Heure d'arrivée", value=pd.to_datetime("18:00").time())
        with col2:
            fin = st.time_input("Heure de départ", value=pd.to_datetime("20:00").time())
        
        bouton_soumettre = st.form_submit_button("Ajouter ma disponibilité")
        
        if bouton_soumettre:
            if nom.strip() == "":
                st.error("N'oubliez pas d'indiquer votre nom !")
            else:
                donnees = charger_donnees()
                donnees.append({
                    "nom": nom.strip(),
                    "jour": jour,
                    "debut": debut.strftime("%H:%M"),
                    "fin": fin.strftime("%H:%M")
                })
                sauvegarder_donnees(donnees)
                st.success(f"Merci {nom}, ta disponibilité pour {jour} a bien été enregistrée !")

    # Afficher qui a déjà participé pour encourager les autres
    donnees_actuelles = charger_donnees()
    if donnees_actuelles:
        participants = sorted(list(set([d["nom"] for d in donnees_actuelles])))
        st.write("### Ils ont déjà répondu :")
        st.write(", ".join(participants))

# ==========================================
# VUE 2 : L'ADMINISTRATEUR (Phase de génération)
# ==========================================
if est_admin:
    st.success("Mode Administrateur activé")
    st.header("⚙️ Panneau de Génération")
    
    donnees = charger_donnees()
    
    # Bouton pour remettre à zéro en fin de semaine
    if st.button("🗑️ Effacer toutes les données (Pour une nouvelle semaine)"):
        sauvegarder_donnees([])
        st.rerun()
        
    st.subheader("Aperçu des réponses reçues")
    if donnees:
        st.dataframe(pd.DataFrame(donnees))
    else:
        st.warning("Personne n'a encore rempli ses disponibilités.")

    st.subheader("Générer le planning final")
    col1, col2 = st.columns(2)
    with col1:
        duree_creneau = st.number_input("Durée d'un créneau (min)", value=30, step=10)
    with col2:
        joueurs_par_creneau = st.number_input("Joueurs par créneau", value=2, min_value=1)
        
    if st.button("🚀 Créer le planning de la semaine", type="primary"):
        if not donnees:
            st.error("Impossible de générer un planning sans joueurs.")
        else:
            jours_semaine = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
            planning_complet = []
            
            # Ce compteur suit le temps de jeu SUR TOUTE LA SEMAINE
            # C'est ce qui garantit que quelqu'un qui a peu joué lundi sera prioritaire mardi
            compteur_global = {d["nom"]: 0 for d in donnees}
            
            for jour in jours_semaine:
                dispos_jour = [d for d in donnees if d["jour"] == jour]
                if not dispos_jour:
                    continue # On passe au jour suivant s'il n'y a personne
                    
                # Définir l'heure de début et de fin de la journée en fonction des joueurs
                heure_min = min([datetime.strptime(d["debut"], "%H:%M") for d in dispos_jour])
                heure_max = max([datetime.strptime(d["fin"], "%H:%M") for d in dispos_jour])
                
                actuel = heure_min
                while actuel + timedelta(minutes=duree_creneau) <= heure_max:
                    fin_creneau = actuel + timedelta(minutes=duree_creneau)
                    
                    # Trouver qui est disponible pour ce créneau précis
                    joueurs_dispos = []
                    for d in dispos_jour:
                        d_deb = datetime.strptime(d["debut"], "%H:%M")
                        d_fin = datetime.strptime(d["fin"], "%H:%M")
                        if d_deb <= actuel and d_fin >= fin_creneau:
                            joueurs_dispos.append(d["nom"])
                            
                    # Enlever les doublons (si quelqu'un a cliqué deux fois)
                    joueurs_dispos = list(set(joueurs_dispos))
                    
                    # Trier les joueurs par leur temps de jeu global (les moins favorisés d'abord)
                    joueurs_dispos.sort(key=lambda x: compteur_global[x])
                    
                    # Assigner les places
                    assignes = joueurs_dispos[:joueurs_par_creneau]
                    for a in assignes:
                        compteur_global[a] += 1
                        
                    planning_complet.append({
                        "Jour": jour,
                        "Horaire": f"{actuel.strftime('%H:%M')} - {fin_creneau.strftime('%H:%M')}",
                        "Joueurs assignés": " | ".join(assignes) if assignes else "Pas de joueurs dispos"
                    })
                    
                    actuel += timedelta(minutes=duree_creneau)
            
            st.header("📅 Planning Officiel")
            if planning_complet:
                st.table(pd.DataFrame(planning_complet))
                
                st.header("📊 Équilibre du temps de jeu (Sur la semaine)")
                df_stats = pd.DataFrame(list(compteur_global.items()), columns=["Joueur", "Créneaux joués"])
                st.bar_chart(df_stats.set_index("Joueur"))
            else:
                st.warning("Les horaires saisis ne permettent pas de créer des créneaux complets.")
