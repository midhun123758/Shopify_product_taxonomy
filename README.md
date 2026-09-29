# 🛒 Shopify Product AI Taxonomy Engine

An enterprise-grade, asynchronous e-commerce automation pipeline designed to ingest massive product catalogs (10,000+ items), deduplicate variants, and autonomously classify products into the **Official Shopify Standard Product Taxonomy** using a **Multi-Agent RAG Pipeline (Google Gemini 1.5 Flash)**.

Includes a high-performance React dashboard for real-time monitoring and 1-click human manual review.

---

## 💻 Tech Stack

- **Backend**: Python, Django REST Framework
- **Asynchronous Task Queue**: Celery, Redis Message Broker
- **Database**: PostgreSQL (utilizing the `pg_trgm` extension for sub-millisecond string similarity searches)
- **AI Engine**: Google Gemini 1.5 Flash (Multi-Modal Vision & Text API)
- **Data Engineering**: Pandas (Variant grouping & deduplication)
- **Frontend**: React 18, Vite, Glassmorphism CSS
- **Infrastructure**: Docker & Docker Compose

---

## ⚡ Core Architecture & Pipeline

### 1. Pandas Bulk Upload & Variant Deduplication (80% Cost Reduction)
When a user uploads 10,000+ SKUs via Excel, the Django backend passes the dataset to a Pandas engine. It strips variant dimensions (Size, Color) and groups identical items into single `ProductFamily` entities. This compresses 10,000 requests into roughly 2,000 API calls before execution begins.

### 2. Multi-Agent RAG Pipeline (Zero Hallucinations)
Instead of forcing a single AI prompt to guess categories, the system uses two specialized agents:
- **Agent 1 (Categorizer) & PostgreSQL RAG:** The database runs a lightning-fast `pg_trgm` fuzzy search across the 14,000+ official Shopify categories. The top 5 candidates are sent to Agent 1, which strictly chooses the best existing node (guaranteeing 100% taxonomy compliance).
- **Agent 2 (Attribute Extractor):** Once categorized, Agent 2 dynamically loads the exact allowed attributes for that node from a simplified JSON schema. It extracts exact values (e.g., Color, Material, Shape) directly from the text and image.

### 3. Asynchronous Concurrency (Celery + Redis)
To prevent server crashes on massive uploads, the 2,000 tasks are dropped into a Redis queue. Background Celery workers process these jobs concurrently in parallel. The workers feature **Exponential Backoff & Auto-Retries** to elegantly handle external AI API rate limits (HTTP 429 Too Many Requests) without failing the batch.

### 4. Interactive React Workstation
- **Catalog Grid (`/catalog`)**: Displays grouped families, Live Processing Statuses, and Confidence Scores.
- **Manual Review System**: If the AI confidence drops below 80%, the product is flagged as `MANUAL_REVIEW`. The UI displays the top 4 alternative PostgreSQL database matches as clickable buttons, allowing a human manager to approve the correct category with a single click.

---

## 🚀 How to Setup on Your System

### 1. Prerequisites
- Docker & Docker Compose
- Node.js v18+ (for frontend)
- Google Gemini API Key

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

## 🛡️ Fault Tolerance & State Recovery
The system is fully stateful. Every product commits its status (`PENDING`, `PROCESSING`, `COMPLETED`, `FAILED`) to PostgreSQL atomically. If the server loses power after processing 6,000 products, clicking "Start Batch" upon reboot will cleanly ignore the finished items and instantly resume from the remaining 4,000 `PENDING` rows.
