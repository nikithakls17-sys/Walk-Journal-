"""Run Base Camp: python -m basecamp"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("basecamp.app.main:app", host="0.0.0.0", port=8000)
