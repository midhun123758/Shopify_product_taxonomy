# Shopify Product AI Taxonomy Classifier

This is an enterprise-grade Django & Celery application designed to ingest **massive** Excel files of unclassified e-commerce products and autonomously map them to the official Shopify Product Taxonomy using Google's Gemini AI.

## 🚀 Architecture: Handling Huge Data Storage & Batching

When dealing with catalogs of 10,000+ products, a standard synchronous API will crash, time out, or hit AI rate limits. This backend solves these exact bottlenecks using a 3-step high-performance pipeline:

### 1. In-Memory Pandas Processing
When an Excel file is uploaded via `POST /api/products/import/`, the backend completely bypasses Python's slow native Excel readers and uses `Pandas`. This allows it to read and clean 10,000+ rows in less than a second.

### 2. Database Bulk Insertion (`bulk_create`)
Instead of hitting the PostgreSQL database with 10,000 individual `INSERT` queries (which would lock the database and take minutes), the system compiles all products into a single Python array and executes a single `Product.objects.bulk_create()`. 
* **Result:** 10,000 products are safely persisted to the database in **under 2 seconds**.

### 3. Asynchronous AI Batching (Celery + Redis)
To avoid getting blocked by Google Gemini's API rate limits, the system does **not** process the products during the HTTP request. 
Instead:
1. The 10,000 products are marked as `PENDING`.
2. The HTTP response immediately returns `201 Created` to the user, freeing up the web server.
3. The system slices the 10,000 products into tiny **batches of 18**.
4. These batches are pushed to a **Redis Message Broker**.
5. Background **Celery Workers** pull these batches one by one and send a single bulk prompt (containing 18 products) to the AI.
6. The AI categorizes all 18 products simultaneously, and the worker updates their status to `COMPLETED` or `MANUAL_REVIEW`.

This asynchronous micro-batching architecture guarantees that the system will never crash, will never hit a rate limit, and can scale to millions of products simply by spinning up more Celery worker containers.

---

## 🛠️ Local Setup & Installation

### Prerequisites
- Docker & Docker Compose
- A Google Gemini API Key

### 1. Configure Environment
Create a `.env` file in the root directory and add your API key:
```env
GEMINI_API_KEY=your_api_key_here
POSTGRES_DB=shopify_db
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
```

### 2. Boot the Infrastructure
This project is fully containerized. Start the PostgreSQL database, Redis broker, Nginx reverse proxy, Django Web server, and Celery workers with a single command:
```bash
docker compose up -d --build
```

### 3. Setup the Database
Once the containers are running, run the initial database migrations to create the tables:
```bash
docker compose exec web python manage.py migrate
```

### 4. Load the Official Shopify Taxonomy
Seed the database with the official Shopify categories:
```bash
docker compose exec web python manage.py seed_categories
```

### 5. Access the API
The Swagger UI Documentation will instantly be available at:
👉 **`http://localhost/api/docs/`**

You can upload your massive Excel files to the `/api/products/import/` endpoint and watch the Celery workers categorize them in real-time!
