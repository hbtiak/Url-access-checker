import asyncio
import json
import time
import httpx
import pandas as pd
import streamlit as st
from google import genai  # NEW SDK IMPORT

st.set_page_config(page_title="URL Availability Checker", page_icon="🔎", layout="wide")

# --------------------------------------------------------------------------- #
# HIDE GITHUB ICON, FORK BUTTON, AND TOP-RIGHT MENU
# --------------------------------------------------------------------------- #
hide_github_style = """
    <style>
    header { visibility: hidden !important; }
    .viewerBadge_container__1QSob { display: none !important; }
    footer { visibility: hidden !important; }
    </style>
"""
st.markdown(hide_github_style, unsafe_allow_html=True)

st.title("🔎 URL Availability Checker")
st.caption("Ensure your VPN is connected before running checks.")

# --------------------------------------------------------------------------- #
# SESSION STATE (Prevents table from disappearing on button click)
# --------------------------------------------------------------------------- #
if "results_df" not in st.session_state:
    st.session_state.results_df = None

# --------------------------------------------------------------------------- #
# Helpers & Async Checker
# --------------------------------------------------------------------------- #
def parse_urls(text: str) -> list[str]:
    urls = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "," in line:
            for cell in line.split(","):
                cell = cell.strip().strip('"').strip("'")
                if cell.startswith(("http://", "https://")):
                    urls.append(cell)
                    break
        elif line.startswith(("http://", "https://")):
            urls.append(line)
    return urls

async def check_one(client: httpx.AsyncClient, url: str, idx: int, method: str, timeout: float) -> dict:
    start = time.perf_counter()
    try:
        if method.upper() == "HEAD":
            resp = await client.head(url, timeout=timeout)
            if resp.status_code in (403, 405, 501):
                resp = await client.get(url, timeout=timeout)
        else:
            resp = await client.get(url, timeout=timeout)

        return {
            "idx": idx, "url": url, "success": 200 <= resp.status_code < 400,
            "status": resp.status_code, "final_url": str(resp.url),
            "redirects": " -> ".join(str(h.url) for h in resp.history),
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "error_type": None, "error_detail": None, "body_snippet": (resp.text or "")[:200],
        }
    
    except httpx.RemoteProtocolError as e:
        return _err(url, idx, start, "RemoteProtocolError", "Server closed connection unexpectedly.")
    except httpx.TimeoutException as e:
        return _err(url, idx, start, "Timeout", str(e))
    except httpx.ConnectError as e:
        msg = str(e)
        kind = "DNS Error" if any(k in msg.lower() for k in ("name", "getaddrinfo", "nodename")) else "Connection Error"
        return _err(url, idx, start, kind, msg)
    except Exception as e:
        return _err(url, idx, start, "Other Error", f"{type(e).__name__}: {e}")

def _err(url, idx, start, kind, detail):
    return {
        "idx": idx, "url": url, "success": False, "status": None,
        "final_url": None, "redirects": "", "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        "error_type": kind, "error_detail": detail, "body_snippet": "",
    }

async def run_checks(urls, concurrency, timeout, method, headers, verify_tls, proxy, progress_cb):
    sem = asyncio.Semaphore(concurrency)
    transport = httpx.AsyncHTTPTransport(retries=2, verify=verify_tls, proxy=proxy)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    
    client_kwargs = {
        "limits": limits, "headers": headers, "follow_redirects": True,
        "transport": transport, "http2": False,
    }

    results = []
    async with httpx.AsyncClient(**client_kwargs) as client:
        async def worker(i, u):
            async with sem:
                return await check_one(client, u, i, method, timeout)

        tasks = [asyncio.create_task(worker(i, u)) for i, u in enumerate(urls)]
        for fut in asyncio.as_completed(tasks):
            results.append(await fut)
            progress_cb(len(results), len(urls))

    results.sort(key=lambda r: r["idx"])
    return results

# --------------------------------------------------------------------------- #
# GEMINI AI ANALYSIS FUNCTION (With Retry & Fallback)
# --------------------------------------------------------------------------- #
def generate_ai_report(failures_df):
    if failures_df.empty:
        return "No failures to analyze."

    error_data = ""
    for _, row in failures_df.head(40).iterrows():
        error_data += f"- URL: {row['url']} | Status: {row['status']} | Error: {row['error_type']} | Detail: {row['error_detail']}\n"

    prompt = f"""
    You are an expert QA and Network Engineer. I ran a bulk URL check.
    Here are the failures:
    
    {error_data}
    
    Please provide a concise, 3-bullet-point report:
    1. What is the likely root cause of these failures? (e.g., VPN drop, server error, auth issue).
    2. Group the failures into logical categories (e.g., "All 404s", "All Timeouts").
    3. What is the recommended next step for the engineering team?
    Do not invent URLs. Be direct.
    """

    try:
        client = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Primary Model
                response = client.models.generate_content(
                    model='gemini-3.8-flash',
                    contents=prompt
                )
                return response.text
            except Exception as e:
                # If it's a 503 (busy), wait and retry
                if "503" in str(e) and attempt < max_retries - 1:
                    time.sleep(3)
                    continue
                # If it fails 3 times on Flash, try Pro as a fallback
                elif attempt == max_retries - 1:
                    response = client.models.generate_content(
                        model='gemini-3.8-pro',
                        contents=prompt
                    )
                    return response.text
                else:
                    raise e
                    
    except Exception as e:
        return f"⚠️ **AI Analysis Failed:** Google's servers are currently busy. Please try again in a few minutes. (Error: {e})"

# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
col_input, col_settings = st.columns([3, 2])

with col_input:
    urls_text = st.text_area("Paste URLs (one per line)", height=250, placeholder="https://google.com/")
    uploaded = st.file_uploader("…or upload a .txt / .csv file", type=["txt", "csv"])

with col_settings:
    st.subheader("Check settings")
    concurrency = st.slider("Concurrency", 1, 100, 10)
    timeout = st.number_input("Timeout (seconds)", 1.0, 120.0, 30.0, 1.0)
    method = st.selectbox("HTTP method", ["GET", "HEAD"], 0)
    proxy_url = st.text_input("Proxy URL (if required by VPN)", placeholder="http://proxy.company.com:8080")
    verify_tls = st.checkbox("Verify TLS certificate", value=False)
    headers_input = st.text_area("Extra headers (JSON)", placeholder='{"User-Agent": "Mozilla/5.0..."}', height=100)

# Parse
raw = urls_text or ""
if uploaded: 
    raw += "\n" + uploaded.read().decode("utf-8", errors="ignore")
urls = parse_urls(raw)

headers = {}
if headers_input.strip():
    try: 
        headers = json.loads(headers_input)
    except Exception as e: 
        st.warning(f"Invalid JSON: {e}")

# Run Button
if st.button("Run checks", type="primary", disabled=not urls):
    progress_bar = st.progress(0.0)
    
    results = asyncio.run(run_checks(
        urls, concurrency, timeout, method, headers, verify_tls, proxy_url or None,
        lambda d, t: progress_bar.progress(d / t)
    ))
    
    # Save the results to Session State so they don't disappear
    df = pd.DataFrame(results)
    df["Result"] = df["success"].apply(lambda x: "✅ SUCCESS" if x else "❌ FAILURE")
    cols = ["url", "Result", "status", "latency_ms", "error_type", "error_detail", "final_url", "redirects"]
    st.session_state.results_df = df[cols]

# --------------------------------------------------------------------------- #
# DISPLAY RESULTS (Reads from Session State)
# --------------------------------------------------------------------------- #
if st.session_state.results_df is not None:
    df = st.session_state.results_df
    
    st.write("### Results")
    
    def color_rows(row):
        color = '#d4edda' if row['Result'] == '✅ SUCCESS' else '#f8d7da'
        return [f'background-color: {color}'] * len(row)

    styled_df = df.style.apply(color_rows, axis=1)
    st.dataframe(styled_df, use_container_width=True, hide_index=True)

    csv = df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="⬇️ Download Report (CSV)",
        data=csv,
        file_name='url_check_report.csv',
        mime='text/csv',
    )
    
    failures = df[df['Result'] == '❌ FAILURE']
    if not failures.empty:
        st.error(f"{len(failures)} URL(s) failed. See details in the table above.")
        
        st.write("---")
        st.subheader("🧠 AI Root-Cause Analysis")
        st.write("Let Google Gemini AI analyze the failure logs and explain *why* they failed.")
        
        if st.button("Analyze Failures with AI"):
            with st.spinner("Google Gemini is analyzing the failures..."):
                ai_report = generate_ai_report(failures)
                st.info(ai_report)
