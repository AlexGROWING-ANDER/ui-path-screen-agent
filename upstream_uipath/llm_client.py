import os
import requests

def send_messages(payload: dict) -> str | None:
    """Call the configured OpenAI-compatible planner or the original proxy."""
    base_url = os.getenv("UIPATH_LLM_BASE_URL", "").strip()
    if base_url:
        from openai import OpenAI

        api_key = os.getenv("UIPATH_LLM_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("UIPATH_LLM_API_KEY is required with UIPATH_LLM_BASE_URL")
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=180.0)
        response = client.chat.completions.create(**payload)
        content = response.choices[0].message.content
        return content if isinstance(content, str) else None

    # URL to your proxy for calling LLMs
    proxy_url = os.getenv("UIPATH_PLANNER_PROXY_URL", "").strip()
    if not proxy_url:
        raise RuntimeError("Set UIPATH_LLM_BASE_URL or UIPATH_PLANNER_PROXY_URL")
    api_key = os.getenv("SERVICE_KEY")
    # Can be directly replaced with code for calling Azure endpoint as in:
    #.env config example :
    # AZURE_OPENAI_API_BASE=YOUR_API_BASE
    # AZURE_OPENAI_DEPLOYMENT=YOUR_DEPLOYMENT
    # AZURE_OPENAI_API_VERSION=YOUR_API_VERSION
    # AZURE_OPENAI_MODEL=gpt-4o-mini
    # AZURE_OPENAI_API_KEY={{YOUR_API_KEY}}
    # AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_API_BASE}/openai/deployments/${AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version=${AZURE_OPENAI_API_VERSION}


    # Load environment variables
    # load_dotenv()
    # api_key = os.getenv('AZURE_OPENAI_API_KEY')
    # openai_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    # #logger.info("Openai endpoint: %s", openai_endpoint)

    # headers = {
    #     "Content-Type": "application/json",
    #     "api-key": api_key
    # }
    # response = requests.post(
    #     openai_endpoint,
    #     headers=headers,
    #     json=payload
    # )
            
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": api_key
    }
    retries = 3
    for attempt in range(retries):
        response = requests.post(proxy_url, headers=headers, json=payload, timeout=180)
        if response.status_code == 200:
            return response.text
    return None
