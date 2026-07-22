import os
import time
import requests
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

# Set up Gemini
import google.generativeai as genai

# Simple global rate limiter state for Gemini Free Tier (15 RPM -> min 4.5 seconds per request)
_last_gemini_call_time = 0.0
GEMINI_COOLDOWN = 4.5  # seconds

def call_llm(prompt: str) -> str:
    provider = os.getenv("LLM_PROVIDER", "gemini").lower()
    
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            print("WARNING: GEMINI_API_KEY not found in env. Falling back to Ollama.")
            return call_ollama(prompt)
        try:
            return call_gemini(prompt, api_key)
        except Exception as e:
            print(f"Error calling Gemini: {e}. Falling back to Ollama.")
            return call_ollama(prompt)
    else:
        return call_ollama(prompt)

def call_ollama(prompt: str) -> str:
    model = os.getenv("OLLAMA_MODEL", "qwen:3b")
    url = "http://localhost:11434/api/generate"
    try:
        resp = requests.post(url, json={
            "model": model,
            "prompt": prompt,
            "stream": False
        }, timeout=60)
        resp.raise_for_status()
        return resp.json()["response"]
    except Exception as e:
        return f"Error calling Ollama: {e}. Please check if Ollama is running at http://localhost:11434."

def call_gemini(prompt: str, api_key: str) -> str:
    global _last_gemini_call_time
    
    # Enforce free-tier rate limit (15 RPM)
    now = time.time()
    elapsed = now - _last_gemini_call_time
    if elapsed < GEMINI_COOLDOWN:
        sleep_time = GEMINI_COOLDOWN - elapsed
        print(f"  [Rate Limiter] Sleeping {sleep_time:.2f}s to respect Gemini Free Tier 15 RPM limit...")
        time.sleep(sleep_time)
        
    genai.configure(api_key=api_key)
    # Using gemini-3.5-flash-lite (fast, active model on current API key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    model = genai.GenerativeModel(model_name)
    response = model.generate_content(prompt)
    
    # Update call timestamp
    _last_gemini_call_time = time.time()
    return response.text
