"""
Project to find and repair (replace) unavailable spotify tracks

Matt Notarangelo
"""


import dotenv
import json
import spotipy

CLIENT_ID = dotenv.get_key(dotenv_path=".env", key_to_get="CLIENT_ID")
CLIENT_SECRET = dotenv.get_key(dotenv_path=".env", key_to_get="CLIENT_SECRET")
REDIRECT_URI = "http://localhost:3000"
BATCH_SIZE = 50


def main():
    sp = spotipy.Spotify(
        auth_manager=spotipy.SpotifyOAuth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope="user-library-read",
        )
    )

    offset = 0
    while True:
        results = sp.current_user_saved_tracks(
            limit=BATCH_SIZE, offset=offset * BATCH_SIZE, market="AU"
        )

        if not results or not results["items"]:
            break

        for idx, item in enumerate(results["items"]):
            if not item["track"]["is_playable"]:
                print(
                    idx + offset * BATCH_SIZE,
                    item["track"]["artists"][0]["name"],
                    "-",
                    item["track"]["name"],
                )

        offset += 1


if __name__ == "__main__":
    main()
