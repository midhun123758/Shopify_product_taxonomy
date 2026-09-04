# Shopify Product AI Taxonomy Classifier & Dashboard

This is an enterprise-grade Django, Celery, and React application designed to ingest **massive** Excel files of unclassified e-commerce products and autonomously map them to the official Shopify Product Taxonomy using a Two-Stage AI architecture powered by Google's Gemini.

## 🚀 The AI Architecture

### 1. Two-Stage Generative AI Pipeline
To prevent AI hallucinations and ensure strict adherence to Shopify's data structure, the AI processing is split into two distinct stages:
* **Stage 1 (Category Mapping):** The AI reads the vendor's messy category and title, and predicts the exact official Shopify Taxonomy path (e.g., `Furniture > Chairs > Kitchen & Dining Room Chairs`).
* **Stage 2 (Attribute Extraction):** Once the official category is locked in, the system fetches the specific required attributes for that exact category (e.g., `Material`, `Color`). It sends a *second* prompt to the AI to extract ONLY those allowed attributes from the product description. 

### 2. In-Memory Taxonomy Engine & Fuzzy Matching
Shopify has **28,927 official categories**. Sending this entire list to the AI would instantly exceed token limits and crash the application. 
Instead, the entire taxonomy is loaded into an ultra-fast Python dictionary memory pool. The AI guesses the category, and a strict Python **Fuzzy Matcher (`difflib`)** intercepts the guess. If the AI's guess is slightly misspelled, the Fuzzy Matcher instantly auto-corrects it to the 100% exact official Shopify category (requiring a 75% accuracy threshold).

### 3. Asynchronous AI Batching & Fault Tolerance
To avoid hitting Google Gemini's API rate limits when processing 10,000+ products:
1. Products are saved to PostgreSQL in bulk (`bulk_create`) and marked as `PENDING`.
2. The backend slices the queue into tiny **batches of 18** and pushes them to a **Redis Message Broker**.
3. Background **Celery Workers** pull these batches and send bulk prompts to the AI.
4. **Exponential Backoff:** If the AI hits a Rate Limit (429) or Server Overload (503), the Celery worker catches the exception and automatically retries with an exponential delay, guaranteeing zero data loss.

---

## 💻 The React Dashboard

The project includes a sleek, modern React + Vite frontend that communicates with the Django API:
* **Live Analytics:** A real-time dashboard showing the number of Pending, Processing, and Completed products.
* **Manual Review Queue:** If the Fuzzy Matcher determines the AI's guess is less than 75% accurate, it safely flags the product as `Needs Review`. The dashboard displays these products along with the AI's alternative suggestions, allowing a human to click and approve the correct category.
* **Queue Control:** "Pause Processing" and "Resume Processing" buttons allow administrators to instantly freeze the Celery queue if AI billing quotas are reached.

---

## 🛠️ Local Setup & Installation

### Prerequisites
- Docker & Docker Compose
- Node.js (for the frontend)
- A Google Gemini API Key

### 1. Configure the Backend Environment
Create a `.env` file in the root `Shopify Product Taxonomy` directory:
```env
GEMINI_API_KEY=your_api_key_here
POSTGRES_DB=shopify_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
```

### 2. Boot the Backend Infrastructure
The backend is fully containerized. Start the PostgreSQL database, Redis broker, Nginx reverse proxy, Django Web server, and Celery workers with a single command:
```bash
docker compose up -d --build
```

### 3. Setup the Database & Seed Data
Run the migrations and seed the official Shopify categories:
```bash
docker compose exec web python manage.py migrate
docker compose exec web python manage.py seed_categories
```
*The backend API will now be running on `http://localhost/api/`*

### 4. Boot the React Frontend
Open a new terminal, navigate to your frontend directory, and start Vite:
```bash
cd shopify_product_frontent/frontend
npm install
npm run dev
```

You can now open the frontend on `http://localhost:5173/`, upload your massive Excel files, and watch the AI categorize them in real-time!
