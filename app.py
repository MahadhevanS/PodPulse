import streamlit as st
import feedparser
import requests
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from urllib.parse import urlencode
from supabase import create_client
from datetime import datetime

# ======================================
# 🔐 CONFIGURATION (Secrets)
# ======================================
CLIENT_ID = st.secrets["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = st.secrets["GOOGLE_CLIENT_SECRET"]
REDIRECT_URI = st.secrets["GOOGLE_REDIRECT_URI"]

supabase = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = [
    "https://www.googleapis.com/auth/generative-language.retriever",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email"
]

# ======================================
# 🔑 HELPERS & DB LOGIC
# ======================================
def get_auth_url():
    params = {
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
        "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent"
    }
    return f"{AUTH_URL}?{urlencode(params)}"

def sync_user_to_db(token_data):
    user_info = requests.get("https://www.googleapis.com/oauth2/v3/userinfo", 
                             headers={"Authorization": f"Bearer {token_data['access_token']}"}).json()
    email = user_info["email"]
    db_data = {"email": email, "refresh_token": token_data.get("refresh_token"), "last_login": datetime.now().isoformat()}
    supabase.table("user_profiles").upsert(db_data).execute()
    return email

# Logic for dynamic source addition
def update_sources_in_db():
    """Pushes the current session_state.source_list to the specific podcast record in Supabase"""
    if "current_podcast_id" in st.session_state:
        supabase.table("podcasts").update({
            "trusted_sources": st.session_state.source_list
        }).eq("id", st.session_state.current_podcast_id).execute()

def add_source():
    new_url = st.session_state.url_input.strip()
    if new_url and new_url not in st.session_state.source_list:
        st.session_state.source_list.append(new_url)
        update_sources_in_db() # 👈 Save immediately
    st.session_state.url_input = ""

def fetch_all_news(rss_sources, limit=5):
    articles = []
    for src in rss_sources:
        feed = feedparser.parse(src)
        for entry in feed.entries[:limit]:
            articles.append(f"TITLE: {entry.get('title')}\nSUMMARY: {entry.get('summary', '')}\nURL: {entry.get('link')}\n")
    return "\n".join(articles)

def generate_episode(access_token, news_pool, theme, focus, trusted_links):
    # CRITICAL FIX: Wrap the raw token in a Credentials object
    creds = Credentials(token=access_token)
    
    # Configure the library with the credentials object
    genai.configure(credentials=creds)

    # Use the 2.0 Flash model for speed and logic
    model = genai.GenerativeModel(model_name="gemini-2.0-flash")

    # Cleaned Prompt: Strictly uses provided RSS content
    prompt = f"""
    You are the 'Executive Producer' for the podcast: "{theme}".
    Your goal for this episode is: "{focus}".

    NEWS POOL (Starting Points):
    {news_pool}

    ADDITIONAL TRUSTED SOURCES:
    {trusted_links}

    TASK:
    1. Select 10 stories in total. Use the NEWS POOL above as your primary lead list, but you are ENCOURAGED to supplement this with 3-4 high-impact stories from the wider web that perfectly fit the show's theme.
    2. For stories outside the News Pool, draw from your internal real-time knowledge of trending events on websites like Reddit, Hacker News, or niche industry blogs.
    3. Format the response as high-energy 'Segment Cards'.
    4. Provide the direct, valid URL for every story. If you are citing a story from your internal knowledge, ensure the URL is the correct official source.
    5. Do NOT hallucinate fake news; only discuss verified events that have actually occurred.

    OUTPUT FORMAT:
    ### 💎 [Segment Title]
    **🎙️ Why It Fits:** [Brief explanation of relevance]
    **📝 Talking Points:** - [Bullet 1]
    
    **🔗 Source:** [URL] - [Bullet 2]
    
    ---
    """

    response = model.generate_content(prompt)
    return response.text

# ======================================
# 🖥️ STREAMLIT UI
# ======================================
st.markdown("""
<style>
div[data-testid="column"] button[class*="st-key-del_"]:hover {{
    color: #ff4b4b !important;
    background-color: transparent !important;
}}
</style>
""", unsafe_allow_html=True)

st.set_page_config("🎙️ PodPulse", layout="wide")

# 1. Auth Flow
if "code" in st.query_params and "access_token" not in st.session_state:
    code = st.query_params["code"]
    token_data = requests.post(TOKEN_URL, data={
        "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI, "grant_type": "authorization_code"
    }).json()
    st.session_state["access_token"] = token_data["access_token"]
    st.session_state["user_email"] = sync_user_to_db(token_data)
    st.query_params.clear()
    st.rerun()

if "access_token" not in st.session_state:
    st.title("🎧 Welcome, Producer")
    st.link_button("🚀 Login with Google", get_auth_url())
    st.stop()

# 2. State Initialization
user_email = st.session_state["user_email"]
if "source_list" not in st.session_state:
    st.session_state.source_list = []

# 2.5 Logic to handle HTML-based deletes
if "delete_idx" in st.query_params:
    idx_to_del = int(st.query_params["delete_idx"])
    if 0 <= idx_to_del < len(st.session_state.source_list):
        st.session_state.source_list.pop(idx_to_del)
        update_sources_in_db()
    # Clear the param so it doesn't keep deleting on refresh
    st.query_params.clear()
    st.rerun()

# 3. Sidebar: Podcast Manager
with st.sidebar:
    st.header("📂 Podcasts Manager")
    podcasts_res = supabase.table("podcasts").select("*").eq("owner_email", user_email).execute()
    podcast_options = {p['podcast_name']: p for p in podcasts_res.data}
    
    selected_name = st.selectbox("Switch Project:", list(podcast_options.keys()) + ["➕ Add New Podcast"])
    
    if selected_name == "➕ Add New Podcast":
        with st.form("new_podcast"):
            new_name = st.text_input("New Podcast Name")
            if st.form_submit_button("Create Project"):
                supabase.table("podcasts").insert({"owner_email": user_email, "podcast_name": new_name}).execute()
                st.rerun()
    else:
        current_p = podcast_options[selected_name]
        st.session_state.source_list = current_p.get('trusted_sources', [])
        st.divider()
        theme = st.text_input("Theme", value=current_p.get('theme', 'General Tech'))
        focus = st.text_input("Goal", value=current_p.get('episode_goal', 'Weekly Updates'))
        
        if st.button("💾 Save Project Config"):
            supabase.table("podcasts").update({
                "theme": theme, "episode_goal": focus, "trusted_sources": st.session_state.source_list
            }).eq("id", current_p['id']).execute()
            st.toast("Project Synced!")

# 4. Main View: Managed Sources UI
st.title(f"🎧 Architect: {selected_name if selected_name != '➕ Add New Podcast' else 'Setup'}")

if selected_name != "➕ Add New Podcast":
    current_p = podcast_options[selected_name]
    st.session_state.current_podcast_id = current_p['id'] # Store ID for syncing
    
    # LOAD data from DB if session is empty (prevents losing data on refresh)
    if not st.session_state.source_list:
        st.session_state.source_list = current_p.get('trusted_sources', [])

    st.subheader("🔗 Managed Sources")
    st.text_input("Add a website or RSS link:", key="url_input", on_change=add_source)

    # Display the list
    if st.session_state.source_list:
        st.write("Current Sources:")
        for i, url in enumerate(st.session_state.source_list):
            # Create two columns: one for the label, one for the button
            cols = st.columns([0.9, 0.1])
            
            with cols[0]:
                # This div creates the dark background box
                st.text_input(
                    "Source URL", 
                    value=url, 
                    key=f"source_url_{i}", 
                    label_visibility="collapsed"
                )
            
            with cols[1]:
                if st.button("X", key=f"del_{i}", type="tertiary", help="Delete source"):
                    st.session_state.source_list.pop(i)
                    update_sources_in_db()
                    st.rerun()

    if st.button("✨ Generate Weekly Intelligence"):
        with st.spinner("Producer is researching the web..."):
            news_pool = fetch_all_news(st.session_state.source_list if st.session_state.source_list else ["https://techcrunch.com/feed/"])
            script = generate_episode(st.session_state["access_token"], news_pool, theme, focus, st.session_state.source_list)
            st.markdown("## 📋 Production Script")
            st.markdown(script)
