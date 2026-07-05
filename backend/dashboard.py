import streamlit as st
import pandas as pd
import numpy as np
import threading
import time
from data import market_data

# Page configuration
st.set_page_config(
    page_title="My First Streamlit App",
    page_icon="📈",
    layout="wide"
)

if "market_started" not in st.session_state:
    threading.Thread(
        target=market_data.start_market_data,
        daemon=True
    ).start()

    st.session_state.market_started = True
# Sidebar
st.sidebar.title("Navigation")
page = st.sidebar.selectbox(
    "Choose a page",
    ["Home", "Dashboard", "About"]
)

# Home Page
if page == "Home":
    st.title("📈 Welcome to Streamlit")
    st.write("This is a simple Streamlit application.")

    name = st.text_input("Enter your name")

    if st.button("Submit"):
        st.success(f"Hello, {name}! 👋")

# Dashboard Page
elif page == "Dashboard":

    st.title("📊 Live Market Dashboard")
    # st.write(market_data.live_market_data)

    placeholder = st.empty()

    while True:

        if market_data.live_market_data:

            df = pd.DataFrame.from_dict(
                market_data.live_market_data,
                orient="index"
            )

            placeholder.dataframe(
                df,
                use_container_width=True
            )

        else:
            placeholder.info("Waiting for Live Market Data...")

        time.sleep(1)
# About Page
else:
    st.title("ℹ️ About")
    st.write("""
    This app is built using Streamlit.

    Features:
    - Sidebar
    - Metrics
    - Table
    - Charts
    - Buttons
    """)

# Footer
st.divider()
st.caption("Made with ❤️ using Streamlit")
import threading
from data import market_data

threading.Thread(
    target=market_data.start_market_data,
    daemon=True
).start()