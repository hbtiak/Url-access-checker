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
    /* 1. Hide the entire top header bar (removes Fork, GitHub icon, and hamburger menu) */
    header { visibility: hidden !important; }
    
    /* 2. Hide the specific viewer badge container as a backup */
    .viewerBadge_container__1QSob { display: none !important; }
    
    /* 3. Hide the Streamlit footer */
    footer { visibility: hidden !important; }
    </style>
"""
st.markdown(hide_github_style, unsafe_allow_html=True)

st.title("🔎 URL Availability Checker")
st.caption("Ensure your VPN is connected before running checks.")

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
        return _err(url, idx, start, "RemoteProtocolError", "Server closed connection unexpectedly. Try lowering concurrency.")
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
    
    transport = httpx.AsyncHTTPTransport(
        retries=2,
        verify=verify_tls,
        proxy=proxy,
    )
    
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    
    client_kwargs = {
        "limits": limits,
        "headers": headers,
        "follow_redirects": True,
        "transport": transport,
        "http2": False,
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
    concurrency = st.slider("Concurrency", 1, 100, 10, help="If you get RemoteProtocolErrors, lower this number.")
    timeout = st.number_input("Timeout (seconds)", 1.0, 120.0, 30.0, 1.0, help="Increased default to 30s for slow product pages.")
    method = st.selectbox("HTTP method", ["GET", "HEAD"], 0)
    
    proxy_url = st.text_input("Proxy URL (if required by VPN)", placeholder="http://proxy.company.com:8080")
    verify_tls = st.checkbox("Verify TLS certificate", value=False, help="Uncheck for corporate VPNs that re-sign certificates.")
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

if st.button("Run checks", type="primary", disabled=not urls):
    progress_bar = st.progress(0.0)
    
    results = asyncio.run(run_checks(
        urls, concurrency, timeout, method, headers, verify_tls, proxy_url or None,
        lambda d, t: progress_bar.progress(d / t)
    ))
    
    df = pd.DataFrame(results)
    df["Result"] = df["success"].apply(lambda x: "✅ SUCCESS" if x else "❌ FAILURE")
    
    cols = ["url", "Result", "status", "latency_ms", "error_type", "error_detail", "final_url", "redirects"]
    df = df[cols]

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
