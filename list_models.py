import os
import requests
from dotenv import load_dotenv
load_dotenv()

# We will grab the GEMINI_API_KEY from the environment
api_key = os.environ.get("GEMINI_API_KEY", "your-gemini-key-here")
if api_key == "your-gemini-key-here":
    print("Warning: Using placeholder key. Set GEMINI_API_KEY!")

url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
response = requests.get(url)

if response.status_code == 200:
    models = response.json().get('models', [])
    print("===== AVAILABLE MODELS FOR YOUR API KEY =====")
    for m in models:
        methods = m.get('supportedGenerationMethods', [])
        if 'generateContent' in methods:
            print(f"- {m.get('name').replace('models/', '')}")
    print("=============================================")
else:
    print(f"API Error {response.status_code}: {response.text}")
