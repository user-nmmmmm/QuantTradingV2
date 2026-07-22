import streamlit as st


def apply_theme():
    """Apply a modern, professional dark theme (GitHub Dark / Linear style)."""
    st.markdown(
        """
        <style>
        /* Import Inter font */
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
        
        /* Base Styles */
        html, body, [class*="css"] {
            font-family: 'Inter', sans-serif;
            color: #C9D1D9; /* Text Color */
        }
        
        /* Backgrounds */
        .stApp {
            background: linear-gradient(135deg, #0D1117 0%, #161B22 100%);
        }
        
        /* Sidebar */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #161B22 0%, #0D1117 100%);
            border-right: 1px solid #30363D;
        }
        
        /* Headers */
        h1, h2, h3, h4, h5, h6 {
            color: #FFFFFF !important;
            font-weight: 600;
            letter-spacing: -0.02em;
            background: linear-gradient(90deg, #58A6FF, #A371F7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        /* Metric Cards */
        [data-testid="stMetric"] {
            background: linear-gradient(135deg, #21262D 0%, #1A1F26 100%);
            border: 1px solid #30363D;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            transition: all 0.3s ease;
            backdrop-filter: blur(10px);
        }
        [data-testid="stMetric"]:hover {
            transform: translateY(-4px);
            border-color: #58A6FF;
            box-shadow: 0 12px 40px rgba(88, 166, 255, 0.2);
        }
        [data-testid="stMetricLabel"] {
            color: #8B949E !important;
            font-size: 14px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        [data-testid="stMetricValue"] {
            color: #58A6FF !important;
            font-size: 32px;
            font-weight: 700;
            background: linear-gradient(90deg, #58A6FF, #79C0FF);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        /* Buttons */
        .stButton > button {
            background: linear-gradient(135deg, #238636 0%, #2EA043 100%);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 12px 24px;
            font-weight: 600;
            font-size: 14px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(35, 134, 54, 0.3);
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .stButton > button:hover {
            background: linear-gradient(135deg, #2EA043 0%, #238636 100%);
            box-shadow: 0 6px 20px rgba(46, 160, 67, 0.4);
            transform: translateY(-2px);
        }
        .stButton > button:active {
            transform: translateY(0);
        }
        
        /* Inputs & Selectboxes */
        .stTextInput input, .stNumberInput input, .stSelectbox div[data-baseweb="select"] {
            background: linear-gradient(135deg, #0D1117 0%, #161B22 100%);
            border: 2px solid #30363D;
            color: #C9D1D9;
            border-radius: 8px;
            padding: 12px;
            font-size: 14px;
            transition: all 0.3s ease;
        }
        .stTextInput input:focus, .stNumberInput input:focus {
            border-color: #58A6FF;
            box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.3), 0 0 20px rgba(88, 166, 255, 0.1);
            outline: none;
        }
        
        /* Dataframes */
        [data-testid="stDataFrame"] {
            border: 1px solid #30363D;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
        }
        
        /* Tabs */
        .stTabs [data-baseweb="tab-list"] {
            gap: 32px;
            border-bottom: 2px solid #30363D;
            padding-bottom: 0px;
        }
        .stTabs [data-baseweb="tab"] {
            height: auto;
            padding: 16px 0;
            background-color: transparent;
            color: #8B949E;
            font-weight: 500;
            border: none;
            margin-bottom: -2px;
            font-size: 16px;
            transition: all 0.3s ease;
        }
        .stTabs [data-baseweb="tab"]:hover {
            color: #C9D1D9;
        }
        .stTabs [aria-selected="true"] {
            color: #58A6FF;
            background-color: transparent;
            border-bottom: 3px solid #58A6FF;
        }
        
        /* Expander */
        .streamlit-expanderHeader {
            background: linear-gradient(135deg, #161B22 0%, #0D1117 100%);
            border: 1px solid #30363D;
            border-radius: 8px;
            color: #C9D1D9;
            font-weight: 500;
            transition: all 0.3s ease;
        }
        .streamlit-expanderHeader:hover {
            border-color: #58A6FF;
        }
        
        /* Progress Bar */
        .stProgress > div > div > div > div {
            background: linear-gradient(90deg, #238636, #2EA043);
            border-radius: 4px;
        }
        
        /* Code Block */
        code {
            color: #E0E0E0;
            background: linear-gradient(135deg, #161B22 0%, #0D1117 100%);
            border-radius: 6px;
            border: 1px solid #30363D;
        }
        
        /* Cards */
        .card {
            background: linear-gradient(135deg, #21262D 0%, #1A1F26 100%);
            border: 1px solid #30363D;
            border-radius: 12px;
            padding: 24px;
            margin: 16px 0;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            backdrop-filter: blur(10px);
            transition: all 0.3s ease;
        }
        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 12px 40px rgba(88, 166, 255, 0.1);
            border-color: #58A6FF;
        }
        
        /* Gradient Text */
        .gradient-text {
            background: linear-gradient(90deg, #58A6FF, #A371F7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-weight: 600;
        }
        
        /* Status Indicators */
        .status-online {
            display: inline-block;
            width: 8px;
            height: 8px;
            background: linear-gradient(135deg, #238636, #2EA043);
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        
        /* Modern Cards with Glass Effect */
        .glass-card {
            background: rgba(33, 38, 45, 0.6);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border: 1px solid rgba(88, 166, 255, 0.2);
            border-radius: 16px;
            padding: 32px;
            margin: 16px 0;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
            transition: all 0.3s ease;
        }
        .glass-card:hover {
            transform: translateY(-6px);
            border-color: rgba(88, 166, 255, 0.4);
            box-shadow: 0 12px 40px rgba(88, 166, 255, 0.15);
        }
        
        /* Gradient Buttons */
        .gradient-button {
            background: linear-gradient(135deg, #58A6FF 0%, #A371F7 100%);
            color: white;
            border: none;
            border-radius: 12px;
            padding: 16px 32px;
            font-weight: 600;
            font-size: 16px;
            transition: all 0.3s ease;
            box-shadow: 0 4px 20px rgba(88, 166, 255, 0.3);
            cursor: pointer;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .gradient-button:hover {
            background: linear-gradient(135deg, #A371F7 0%, #58A6FF 100%);
            box-shadow: 0 6px 25px rgba(163, 113, 247, 0.4);
            transform: translateY(-2px);
        }
        
        /* Modern Input Fields */
        .modern-input {
            background: rgba(13, 17, 23, 0.8);
            border: 2px solid rgba(88, 166, 255, 0.3);
            border-radius: 12px;
            color: #C9D1D9;
            padding: 16px;
            font-size: 16px;
            transition: all 0.3s ease;
            backdrop-filter: blur(10px);
        }
        .modern-input:focus {
            border-color: #58A6FF;
            box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.3), 0 0 30px rgba(88, 166, 255, 0.2);
            outline: none;
            background: rgba(13, 17, 23, 0.9);
        }
        
        /* Chart Containers */
        .chart-container {
            background: rgba(33, 38, 45, 0.4);
            border: 1px solid rgba(88, 166, 255, 0.1);
            border-radius: 16px;
            padding: 24px;
            margin: 16px 0;
            backdrop-filter: blur(10px);
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
        }
        
        /* Section Headers */
        .section-header {
            background: linear-gradient(90deg, #58A6FF, #A371F7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            font-size: 24px;
            font-weight: 700;
            margin: 24px 0;
            letter-spacing: -0.5px;
        }
        
        /* Animated Background Elements */
        .floating-element {
            position: absolute;
            border-radius: 50%;
            background: linear-gradient(135deg, rgba(88, 166, 255, 0.1), rgba(163, 113, 247, 0.1));
            animation: float 6s ease-in-out infinite;
        }
        
        @keyframes float {
            0%, 100% { transform: translateY(0px) rotate(0deg); }
            50% { transform: translateY(-20px) rotate(180deg); }
        }
        
        /* Loading Animation */
        .loading-spinner {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid rgba(88, 166, 255, 0.3);
            border-radius: 50%;
            border-top-color: #58A6FF;
            animation: spin 1s ease-in-out infinite;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        /* Enhanced Status Indicators */
        .status-indicator {
            display: inline-flex;
            align-items: center;
            padding: 8px 16px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 500;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: all 0.3s ease;
        }
        
        .status-online {
            background: linear-gradient(135deg, rgba(35, 134, 54, 0.2), rgba(46, 160, 67, 0.1));
            color: #2EA043;
            box-shadow: 0 2px 10px rgba(46, 160, 67, 0.3);
        }
        
        .status-offline {
            background: linear-gradient(135deg, rgba(207, 34, 46, 0.2), rgba(248, 81, 73, 0.1));
            color: #F85149;
            box-shadow: 0 2px 10px rgba(248, 81, 73, 0.3);
        }
        
        .status-warning {
            background: linear-gradient(135deg, rgba(158, 106, 0, 0.2), rgba(212, 168, 0, 0.1));
            color: #D4A800;
            box-shadow: 0 2px 10px rgba(212, 168, 0, 0.3);
        }
        
        .status-indicator:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
        }
        
        /* Animated Gradient Background */
        .animated-gradient {
            background: linear-gradient(-45deg, #0D1117, #161B22, #21262D, #30363D);
            background-size: 400% 400%;
            animation: gradientShift 8s ease infinite;
        }
        
        @keyframes gradientShift {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        
        </style>
    """,
        unsafe_allow_html=True,
    )


def get_plotly_layout():
    """Return a modern dark template for Plotly."""
    return dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, sans-serif", color="#C9D1D9"),
        xaxis=dict(
            gridcolor="#30363D",
            zerolinecolor="#30363D",
            showgrid=True,
            showline=True,
            linecolor="#30363D",
        ),
        yaxis=dict(
            gridcolor="#30363D",
            zerolinecolor="#30363D",
            showgrid=True,
            showline=True,
            linecolor="#30363D",
        ),
        legend=dict(
            bgcolor="rgba(0,0,0,0)",
            bordercolor="#30363D",
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        margin=dict(l=40, r=40, t=40, b=40),
        colorway=[
            "#58A6FF",
            "#238636",
            "#A371F7",
            "#F0883E",
            "#F78166",
            "#3FB950",
        ],  # GitHub Dark Palette
    )
