# 🛒 Shopify Product AI Taxonomy Classifier

An intelligent e-commerce automation tool designed to ingest massive product catalogs (10,000+ items), group product color/size variations into parent families, autonomously classify them into official **Shopify Standard Product Taxonomy** categories using **Google Gemini 3.6 Flash AI**, and provide a 3-level interactive web dashboard for human review and attribute extraction.

---

## 💻 Tech Stack

- **Backend**: Python 3.11, Django 5.0, Django REST Framework
- **Task Queue & Broker**: Celery, Redis
- **Database**: PostgreSQL (with `pg_trgm` trigonometric similarity extension)
- **AI Engine**: Google Gemini API (`gemini-3.6-flash` Multimodal)
- **Frontend**: React 18, Vite, React Router DOM, Glassmorphism CSS
- **Containerization & Proxy**: Docker, Docker Compose, Nginx

---

## ⚡ How It Works (Core Pipeline)

1. **Pandas Bulk Upload & Variant Normalization**:
   - Ingests 10,000+ Excel SKUs in seconds using `pandas` and `bulk_create`.
   - Cleans variant titles (e.g., removing `- Black`, `- 86"`) to aggregate SKUs into **Parent Product Families**, cutting AI API token costs by **80%+**.

2. **Multimodal Gemini AI Classification & Attribute Extraction**:
   - In a single pass, Gemini analyzes both the product **Visual Image** and text details (`Title`, `Brand`, `Description`).
   - Predicts the Shopify taxonomy path and extracts structured JSON attributes (`material`, `shape`, `size`, `set_includes`, `brand`).

3. **PostgreSQL `pg_trgm` Fuzzy Matching**:
   - Matches AI output against 28,000+ official Shopify taxonomy database nodes using trigonometric similarity (`similarity(name, %s)`).
   - Items with high confidence (≥90%) auto-complete; items with <60% confidence are safely routed to **Manual Review**.

4. **3-Level Interactive Drill-Down Workstation**:
   - **Level 1 (`/catalog`)**: Product Family Catalog displaying grouped items, status badges, and category paths.
   - **Level 2 (`/families/:id`)**: Family Details Page listing all child SKUs, colors, and variant thumbnails.
   - **Level 3 (`/products/:id`)**: Product AI Analysis Page showing high-res images, extracted attributes, and **1-Click AI Category Suggestions (`+ Apply`)**.

5. **Completed Items Dashboard & CSV Export**:
   - Real-time dashboard (`/completed`) with one-click CSV export ready for direct import into Shopify admin.

---

## 🚀 How to Setup on Your System

### 1. Prerequisites
- Docker & Docker Compose
- Node.js v18+ (for frontend)
- Google Gemini API Key

---

### 2. Backend Setup (Docker)

1. **Create `.env` file** in the root directory:
   ```env
   GEMINI_API_KEY=your_gemini_api_key_here
   POSTGRES_DB=shopify_db
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=postgres
   POSTGRES_HOST=db
   POSTGRES_PORT=5432
   ```

2. **Boot infrastructure containers**:
   ```bash
   docker compose up -d --build
   ```

3. **Run database migrations**:
   ```bash
   docker compose exec web python manage.py migrate
   ```

4. **Seed official Shopify categories**:
   ```bash
   docker compose exec web python manage.py seed_categories
   ```
   *(Backend API running at `http://localhost/api/`)*

---

### 3. Frontend Setup (React + Vite)

1. Navigate to the frontend directory:
   ```bash
   cd shopify_product_frontent/frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Start Vite dev server:
   ```bash
   npm run dev
   ```
   *(Frontend workstation running at `http://localhost:5173/`)*

---

## 🧪 Run Business Logic Tests

```bash
docker compose exec web python test.py
```
