import asyncio
import json
import time
import httpx
import pandas as pd
import streamlit as st

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
# SESSION STATE
# --------------------------------------------------------------------------- #
if "results_df" not in st.session_state:
    st.session_state.results_df = None

# --------------------------------------------------------------------------- #
# NEW: LOCAL AI SMART DIAGNOSTICS ENGINE
# --------------------------------------------------------------------------- #
def get_ai_diagnosis(row):
    """A local AI engine that interprets the HTTP errors into human-readable insights."""
    status = row["status"]
    error = row["error_type"]
    detail = str(row["error_detail"])
    snippet = str(row["body_snippet"]).lower()

    if row["success"]:
        return "✅ Page loaded successfully."

    # AI Logic 1: Network / VPN Issues
    if error == "DNS Error":
        return "🧠 AI Insight: The domain could not be resolved. The VPN is likely disconnected or split-tunneling is blocking this host."
    if error == "Timeout":
        return "🧠 AI Insight: The server took too long to respond. The VPN connection may be unstable, or the backend is hanging."
    if error == "Connection Error":
        return "🧠 AI Insight: The server actively refused the connection. Check if the internal firewall is blocking Python traffic."
    
    # AI Logic 2: HTTP Status Code Analysis
    if status == 403:
        return "🧠 AI Insight: Access Forbidden. The URL exists, but the server blocked the request (likely missing auth cookies)."
    if status == 404:
        return "🧠 AI Insight: Resource Not Found. The product ID or page has been deleted from the catalog."
    if status == 500:
        return "🧠 AI Insight: Internal Server Error. The backend application (JSP/Java) has crashed or thrown an exception."
    if status == 502 or status == 504:
        return "🧠 AI Insight: Bad Gateway. The proxy server received an invalid response from the upstream application."
    
    # AI Logic 3: Content Analysis (Soft 404s)
    if status == 200 and ("not found" in snippet or "error" in snippet or "exception" in snippet):
        return "🧠 AI Insight: Soft 404 detected. The server returned 200 OK, but the page body contains error text."

    return "🧠 AI Insight: Unclassified error. Requires manual review."

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
    
    # NEW: Apply the AI Diagnostics to every row
    df["AI Diagnostics"] = df.apply(get_ai_diagnosis, axis=1)
    
    cols = ["url", "Result", "AI Diagnostics", "status", "latency_ms", "error_type", "error_detail", "final_url", "redirects"]
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
