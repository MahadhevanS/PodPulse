import streamlit as st
import feedparser
import requests
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from urllib.parse import urlencode
from supabase import create_client
from datetime import datetime

# ======================================
# 🔐 CONFIGURATION
# ======================================
CLIENT_ID = st.secrets["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = st.secrets["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = st.secrets["GOOGLE_REDIRECT_URI"]

supabase = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/generative-language.retriever", "openid", "https://www.googleapis.com/auth/userinfo.email"]

# ======================================
# 🔑 HELPERS & DB LOGIC
# ======================================
def get_auth_url():
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI, "response_type": "code", "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent"}
    return f"{AUTH_URL}?{urlencode(params)}"

def sync_user_to_db(token_data):
    user_info = requests.get("https://www.googleapis.com/oauth2/v3/userinfo", headers={"Authorization": f"Bearer {token_data['access_token']}"}).json()
    email = user_info["email"]
    db_data = {"email": email, "refresh_token": token_data.get("refresh_token"), "last_login": datetime.now().isoformat()}
    supabase.table("user_profiles").upsert(db_data).execute()
    return email

def update_sources_in_db():
    if "current_podcast_id" in st.session_state:
        supabase.table("podcasts").update({"trusted_sources": st.session_state.source_list}).eq("id", st.session_state.current_podcast_id).execute()

def add_source():
    new_url = st.session_state.url_input.strip()
    if new_url and new_url not in st.session_state.source_list:
        st.session_state.source_list.append(new_url)
        update_sources_in_db()
    st.session_state.url_input = ""

def fetch_all_news(rss_sources, limit=5):
    articles = []
    for src in rss_sources:
        try:
            feed = feedparser.parse(src)
            for entry in feed.entries[:limit]:
                articles.append(f"TITLE: {entry.get('title')}\nSUMMARY: {entry.get('summary', '')}\nURL: {entry.get('link')}\n")
        except: continue
    return "\n".join(articles)

# ======================================
# 🎙️ MULTILINGUAL GENERATION LOGIC
# ======================================
def generate_episode(access_token, news_pool, theme, focus, target_lang):
    creds = Credentials(token=access_token)
    genai.configure(credentials=creds)
    model = genai.GenerativeModel(model_name="gemini-2.0-flash")

    prompt = f"""
    You are a professional Multilingual Podcast Producer.
    
    INTENT (User provided these in native script):
    - Theme: {theme}
    - Focus: {focus}

    CONTEXT:
    Research the following NEWS POOL and other websites on internet. Then select 10 stories.
    {news_pool}

    OUTPUT RULES:
    1. Write the final script entirely in the {target_lang} script.
    2. If the user input was in a native script, honor that specific cultural context. don't restrict to regional boundaries; include contents apart from their region as well if relevant. 
    3. For stories outside the News Pool, draw from your internal real-time knowledge of trending events on websites like Reddit, Hacker News, or niche industry blogs on the internet.
    4. Format the response as high-energy 'Segment Cards'.
    5. Do NOT hallucinate fake news; only discuss verified events that have actually occurred. MUST provide the URLs as a proof for the user to verify the content    
    6. Provide the direct, valid URL for every story. If you don't have an URL don't pick that story. URLs must be in english and no explanation is needed about the URL.
    7. No translations or transcriptions in English or other languages; the entire output must be in {target_lang}.

    CRITICAL: You must translate BOTH the content and the structural labels (headings) 
    into {target_lang}. Use the following mapping for the structure:
    - "Why It Fits" -> [Translate this to {target_lang}]
    - "Talking Points" -> [Translate this to {target_lang}]
    - "Source" -> [Translate this to {target_lang}]

    OUTPUT FORMAT (in {target_lang}):
    ### 💎 [Segment Title]

    **🎙️ Why It Fits:** [Context]

    **📝 Talking Points:** - [Bullet points]

    **🔗 Source:** [URL]

    ---
    """
    response = model.generate_content(prompt)
    return response.text

# ======================================
# 🖥️ STREAMLIT UI
# ======================================
st.set_page_config("🎙️ PodPulse", layout="wide")

# 1. Auth Logic (simplified for brevity)
if "code" in st.query_params and "access_token" not in st.session_state:
    code = st.query_params["code"]
    token_data = requests.post(TOKEN_URL, data={"code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"}).json()
    st.session_state["access_token"] = token_data["access_token"]
    st.session_state["user_email"] = sync_user_to_db(token_data)
    st.query_params.clear()
    st.rerun()

if "access_token" not in st.session_state:
    st.title("🎧 Welcome, Producer")
    st.link_button("🚀 Login with Google", get_auth_url())
    st.stop()

# 2. Sidebar: Project Management
with st.sidebar:
    st.header("📂 Podcast Settings")
    podcasts_res = supabase.table("podcasts").select("*").eq("owner_email", st.session_state["user_email"]).execute()
    podcast_options = {p['podcast_name']: p for p in podcasts_res.data}
    
    selected_name = st.selectbox("Project:", list(podcast_options.keys()) + ["➕ New Podcast"])
    
    if selected_name != "➕ New Podcast":
        curr = podcast_options[selected_name]
        st.session_state.current_podcast_id = curr['id']
        st.session_state.source_list = curr.get('trusted_sources', [])
        
        st.divider()
        # Allows native characters (Unicode)
        theme = st.text_input("Theme", value=curr.get('theme', ''))
        focus = st.text_area("Goal", value=curr.get('episode_goal', ''))
        
        # PERSISTED LANGUAGE SELECTOR
        standard_langs = ["English", "Tamil", "Spanish", "French", "Hindi", "German", "Japanese"]

        # 1. Selectbox with an "Other" option
        # Check if the saved language is in our standard list
        db_lang = curr.get('target_language', 'English')
        default_index = standard_langs.index(db_lang) if db_lang in standard_langs else len(standard_langs)

        selection = st.selectbox(
            "Output Language", 
            standard_langs + ["Other..."], 
            index=default_index
        )

        # 2. Show a text input ONLY if "Other..." is selected
        if selection == "Other...":
            # If the DB already has a custom language, use it as the default text
            custom_val = db_lang if db_lang not in standard_langs else ""
            target_lang = st.text_input("Type your language:", value=custom_val, placeholder="e.g. Telugu, Arabic, etc.")
        else:
            target_lang = selection
        
        if st.button("💾 Save & Sync"):
            supabase.table("podcasts").update({
                "theme": theme, 
                "episode_goal": focus, 
                "language": target_lang, 
                "trusted_sources": st.session_state.source_list
            }).eq("id", curr['id']).execute()
            st.success("Config Synced!")

# 3. Main View
if selected_name == "➕ New Podcast":
    with st.form("new_p"):
        name = st.text_input("Name")
        if st.form_submit_button("Create"):
            supabase.table("podcasts").insert({"owner_email": st.session_state["user_email"], "podcast_name": name}).execute()
            st.rerun()
else:
    st.title(f"🎙️ Architect: {selected_name}")
    st.subheader("🔗 Managed Sources")
    st.text_input("Add Source:", key="url_input", on_change=add_source)
    
    for i, url in enumerate(st.session_state.get('source_list', [])):
        c1, c2 = st.columns([0.9, 0.1])
        c1.caption(url)
        if c2.button("X", key=f"del_{i}"):
            st.session_state.source_list.pop(i)
            update_sources_in_db()
            st.rerun()

    if st.button("✨ Generate Episode Intelligence"):
        with st.spinner(f"Processing in {target_lang}..."):
            news = fetch_all_news(st.session_state.source_list)
            script = generate_episode(st.session_state["access_token"], news, theme, focus, target_lang)
            st.markdown(script)
