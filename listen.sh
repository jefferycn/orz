#!/bin/ash
if [[ -z "$TMDB_API_KEY" ]]; then
    echo "Error: TMDB_API_KEY is not set. Please export it before running this script."
    exit 1
fi

WATCH_DIR="/media"
EVENT_FILE="/tmp/orz_events.tmp"
EVENT_FILE_UNIQUE="/tmp/orz_events_unique.tmp"
LOCK_FILE="/tmp/orz_lock"

touch "$EVENT_FILE"
monitor_folders() {
inotifywait -m -r -e create,move --format '%w|%f|%e' "$WATCH_DIR" | while IFS="|" read -r fullpath file action; do
    case "$action" in
        CREATE,ISDIR)
            echo "Folder '${fullpath}${file}' was created. Running orz create."
            orz create "${fullpath}${file}"
            ;;
        CREATE|MOVED_TO)
            echo "$fullpath" >> "$EVENT_FILE"
            ;;
          MOVED_FROM)
            ;;
        *)
            echo "Unhandled action: $action on file '$fullpath'"
            ;;
    esac
done
}

process_folders() {
    while true; do
        sleep 2
        (
            sleep 2
            flock -n 9 || exit 1
            sort -u "$EVENT_FILE" > "$EVENT_FILE_UNIQUE"
            echo -n "" > "$EVENT_FILE"
            while IFS= read -r uniqueFolder; do
                echo "Folder '$uniqueFolder' was changed. Running orz folder."
                orz folder "$uniqueFolder"
            done < "$EVENT_FILE_UNIQUE"
            echo -n "" > "$EVENT_FILE_UNIQUE"
        ) 9>"$LOCK_FILE"
    done
}

monitor_folders &
process_folders &

wait
