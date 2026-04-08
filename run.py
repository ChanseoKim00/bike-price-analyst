from dotenv import load_dotenv
load_dotenv()

from app import create_app

app = create_app()

if __name__ == "__main__":
    import os
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
