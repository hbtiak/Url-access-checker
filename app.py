import asyncio
import json
import time
from typing import Optional

import httpx
import pandas as pd
import streamlit as st

st.set_page_config(page_title="URL Availability Checker", page_icon="🔎", layout="wide")
st.title("🔎 URL Availability Checker")

# --------------------------------------------------------------------------- #
# Helpers & Async Checker (Same as before, with proxy support added)
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
    except httpx.TimeoutException as e:
        return _err(url, idx, start, "timeout", str(e))
    except httpx.ConnectError as e:
        msg = str(e)
        kind = "dns_error" if any(k in msg.lower() for k in ("name", "getaddrinfo", "nodename")) else "connect_error"
        return _err(url, idx, start, kind, msg)
    except Exception as e:
        return _err(url, idx, start, "other", f"{type(e).__name__}: {e}")

def _err(url, idx, start, kind, detail):
    return {
        "idx": idx, "url": url, "success": False, "status": None,
        "final_url": None, "redirects": "", "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        "error_type": kind, "error_detail": detail, "body_snippet": "",
    }

async def run_checks(urls, concurrency, timeout, method, headers, verify_tls, proxy, progress_cb):
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    
    # Configure proxy if provided
    client_kwargs = {
        "limits": limits,
        "headers": headers,
        "verify": verify_tls,
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy
    else:
        client_kwargs["trust_env"] = True  # Try to use system proxy if no explicit proxy provided

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
vpn_required = st.checkbox("VPN required for these URLs", value=True)

col_input, col_settings = st.columns([3, 2])
with col_input:
    urls_text = st.text_area("Paste URLs (one per line)", height=220, placeholder="https://www.google.com/")
    uploaded = st.file_uploader("…or upload a .txt / .csv file", type=["txt", "csv"])

with col_settings:
    st.subheader("Check settings")
    concurrency = st.slider("Concurrency", 1, 200, 50)
    timeout = st.number_input("Timeout (seconds)", 1.0, 120.0, 15.0, 1.0)
    method = st.selectbox("HTTP method", ["HEAD", "GET"], 1) # Default to GET for internal apps
    
    # --- NEW PROXY SETTING ---
    proxy_url = st.text_input("Proxy URL (if required by VPN)", placeholder="http://proxy.company.com:8080")
    
    verify_tls = st.checkbox("Verify TLS certificate", value=False, help="Uncheck for corporate VPNs that re-sign certificates.")
    headers_input = st.text_area("Extra headers (JSON)", placeholder='{"User-Agent": "...", "Cookie": "..."}', height=100)

# Parse
raw = urls_text or ""
if uploaded: raw += "\n" + uploaded.read().decode("utf-8", errors="ignore")
urls = parse_urls(raw)

headers = {}
if headers_input.strip():
    try: headers = json.loads(headers_input)
    except Exception as e: st.warning(f"Invalid JSON: {e}")

if st.button("Run checks", type="primary", disabled=not urls):
    progress = st.progress(0.0)
    
    results = asyncio.run(run_checks(
        urls, concurrency, timeout, method, headers, verify_tls, proxy_url or None,
        lambda d, t: progress.progress(d / t)
    ))
    
    df = pd.DataFrame(results)
    st.dataframe(df, use_container_width=True)
    
    # Show failure details clearly
    failures = df[~df['success']]
    if not failures.empty:
        st.error(f"{len(failures)} URL(s) failed. Check the 'error_detail' column.")
