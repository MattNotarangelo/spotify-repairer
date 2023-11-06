"""
Project to find and repair (replace) unavailable spotify tracks

Matt Notarangelo
"""


import dotenv
import json
import spotipy
from blessed import Terminal

CLIENT_ID = dotenv.get_key(dotenv_path=".env", key_to_get="CLIENT_ID")
CLIENT_SECRET = dotenv.get_key(dotenv_path=".env", key_to_get="CLIENT_SECRET")
REDIRECT_URI = "http://localhost:3000"
BATCH_SIZE = 50


def _write_json(data):
    with open("out.json", "w") as f:
        json.dump(data, f)


def login() -> spotipy.Spotify:
    sp = spotipy.Spotify(
        auth_manager=spotipy.SpotifyOAuth(
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            scope="user-library-read",
        )
    )
    return sp


def draw_home_screen():
    print(term.black_on_darkkhaki(term.center("select an option")))
    print("1. Repair liked songs")
    print("2. Repair playlist")
    print("x. Exit")
    print(term.move_down(1))

    with term.cbreak():
        inp = term.inkey()
        if inp == "x":
            exit(0)
        elif inp == "1":
            repair_liked_songs()
        elif inp == "2":
            repair_playlist()


def repair_playlist():
    print(term.black_on_darkkhaki(term.center("found playlists")))

    playlists = sp.current_user_playlists()

    for playlist in playlists["items"]:
        print(f"Finding songs from playlist - {playlist['name']}")
        search_for_songs(sp.playlist_tracks, {"playlist_id": playlist["id"]})


def repair_liked_songs():
    print("Finding songs from liked songs")
    search_for_songs(sp.current_user_saved_tracks, {})


def search_for_songs(func, search_args):
    offset = 0
    while True:
        results = func(
            **search_args, limit=BATCH_SIZE, offset=offset * BATCH_SIZE, market="AU"
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

    print(term.move_down(1))


def clear_screen():
    print(term.home + term.clear)


sp = login()
term = Terminal()


def main():
    clear_screen()
    while True:
        draw_home_screen()


if __name__ == "__main__":
    main()
