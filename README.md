# 🐍 EKVS Food Decider — Backend API

FastAPI backend API powering the EKVS Food Decider application. Provides endpoints for meal suggestions, place & item management, eating history, and group voting polls with MongoDB sync.

## 🚀 Local Development

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and configure your environment variables:
   ```env
   MONGO_URI=mongodb+srv://<user>:<password>@cluster.mongodb.net/
   MONGO_DB_NAME=food_decider
   DEVICE_OWNER=me
   ```

3. Run the development server:
   ```bash
   python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

4. Verify API at `http://localhost:8000/` or view interactive docs at `http://localhost:8000/docs`.

---

## ☁️ Deploying to Render

To host this backend on **Render**:

1. Create a new Git repository from this `backend` folder and push to GitHub/GitLab.
2. In Render Dashboard (`dashboard.render.com`), click **New +** -> **Web Service**.
3. Connect your backend Git repository.
4. Configure service settings:
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Under **Environment Variables**, add:
   - `MONGO_URI` (your MongoDB Atlas connection string)
   - `MONGO_DB_NAME` (`food_decider`)
   - `DEVICE_OWNER` (`server`)
6. Deploy the web service! Your backend will be available at `https://<your-app-name>.onrender.com`.
