# What is orz?
Automatically rename TV series and Anime series media files and subtitle files based on TMDB api.

## What to solve?

When you have your own Jellyfin server, you may have a lot of media files and subtitle files. Most of the time, those files can be automatically recognized by Jellyfin, but sometimes it will fail with strange reasons. I am tired of renaming them one by one.

## How it works?

- `orz` listens the directory you specified.
- Create a folder with the series name, for example `Tsukigakirei (2017)`.
- `orz` tries grab the metadata from TMDB and save `tvshow.nfo` to the folder and generates seasons folder.
- When media files and subtitle files were moved or created under the session folder, `orz` will rename them based on the metadata.

## How to use?

`TMDB_API_KEY` is required. You can get it from [TMDB](https://www.themoviedb.org/documentation/api).
`/media` folder is monitored by `orz`, mount your media folder under `/media`.

You can either build from source or use pre-built docker image.

build from source:
```shell
docker build -t orz .
```

Use pre-built docker image:
```shell
docker pull jefferyf/orz
```

Run docker image:
```shell
docker run -d --name orz \
    -v /path/to/anime:/media/anime \
    -v /path/to/tv-series:/media/tv-series \
    -e TMDB_API_KEY=your_api_key \
    orz
```
