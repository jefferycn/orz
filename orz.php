#!/usr/bin/env php
<?php

if ($argc < 2) {
    showHelp();
    exit(1);
}

$command = $argv[1];

switch ($command) {
    case 'folder':
        if ($argc < 3) {
            echo "Missing parameters for 'folder' command.\n";
            showHelp();
            exit(1);
        }
        $path = $argv[2];
        $nfoPath = $path . '/../tvshow.nfo';
        if (file_exists($nfoPath)) {
            $xml = simplexml_load_file($nfoPath);
            $showId = (string)$xml->tmdbid;
            $showTitle = (string)$xml->title;
            $seasonNumber = getSeasonNumberFromFolder($path);
            if ($showId && isset($seasonNumber)) {
                renameMediaFiles(
                    $path,
                    $showTitle,
                    getShowEpisodes($showId, $seasonNumber)
                );
            } else {
                echo "Season folder not detected.\n";
            }
        } else {
            echo "No tvshow.nfo found.\n";
        }
        break;

    case 'create':
        if ($argc < 3) {
            echo "Missing parameters for 'create' command.\n";
            showHelp();
            exit(1);
        }
        $path = $argv[2];
        $title = getTitle($path);
        $year = getYear($path);
        if ($title && $year) {
            require_once 'vendor/autoload.php';
            $client = require_once('config.php');
            $response = $client->getSearchApi()->searchTv($title);
            if ($data = getExactMatch($response, $title, $year)) {
                generateNfo($data, $path);
                generateSessionFolders($client->getTvApi()->getTvshow($data['id']), $path);
            }
        } else {
            echo "$title was ignored.\n";
        }
        break;

    default:
        echo "Unknown command '$command'.\n";
        showHelp();
        exit(1);
}

function getTitle($path): string
{
    $folder = basename($path);
    $pos = strrpos($folder, "(");
    return ($pos !== false) ? trim(substr($folder, 0, $pos)) : $folder;
}

function getYear($path)
{
    $folder = basename($path);
    $pos = strrpos($folder, "(");
    return ($pos !== false) ? substr($folder, $pos + 1, 4) : false;
}

function getExactMatch($response, $title, $year)
{
    if ($response['total_results'] > 1) {
        foreach ($response['results'] as $result) {
            if (strcasecmp($result['name'], $title) === 0 &&
                substr($response['results'][0]['first_air_date'], 0, 4) === $year) {
                return $result;
            }
        }
    } else {
        if (substr($response['results'][0]['first_air_date'], 0, 4) === $year) {
            return $response['results'][0];
        } else {
            echo "Year doesn't match.\n";
            return false;
        }
    }
}

function showHelp()
{
    echo "Usage: orz <command> [arguments]\n\n";
    echo "Available commands:\n";
    echo "  orz folder {path}       - Rename season files in {path}\n";
    echo "  orz create {path}       - Create show nfo and season folders\n\n";
}

function generateNfo($showData, $savePath)
{
    $xml = new SimpleXMLElement('<?xml version="1.0" encoding="UTF-8"?><tvshow></tvshow>');
    $xml->addChild('title', htmlspecialchars($showData['name']));
    $xml->addChild('originaltitle', htmlspecialchars($showData['original_name']));
    $xml->addChild('tmdbid', $showData['id']);
    $xml->addChild('plot', htmlspecialchars($showData['overview']));
    $xml->addChild('rating', $showData['vote_average']);
    $xml->addChild('votes', $showData['vote_count']);
    $xml->addChild('year', substr($showData['first_air_date'], 0, 4));
    $xml->addChild('premiered', $showData['first_air_date']);

    file_put_contents($savePath . '/tvshow.nfo', $xml->asXML());
}

function generateSessionFolders($showData, $basePath)
{
    if (empty($showData['seasons'])) {
        return;
    }

    foreach ($showData['seasons'] as $season) {
        $seasonNumber = $season['season_number'];
        $folderName = ($seasonNumber === 0) ? "Specials" : sprintf("Season %02d", $seasonNumber);
        $seasonFolderPath = $basePath . DIRECTORY_SEPARATOR . $folderName;
        if (!file_exists($seasonFolderPath)) {
            mkdir($seasonFolderPath, 0777, true);
        }
    }
}

function getSeasonNumberFromFolder($folderName)
{
    $folderName = basename($folderName);
    if (preg_match('/^season\s*(\d+)/i', $folderName, $matches)) {
        return $matches[1];
    } else if (strcasecmp($folderName, "Specials") === 0) {
        return "0";
    }
    return null;
}


function getShowEpisodes($showId, $seasonNumber): array
{
    require_once 'vendor/autoload.php';
    $client = require_once('config.php');
    $show = $client->getTvApi()->getTvshow($showId);
    $seriesName = $show['name'];

    $season = $client->getTvSeasonApi()->getSeason($showId, $seasonNumber);

    $episodes = [];
    foreach ($season['episodes'] as $episode) {
        $episodeNumber = $episode['episode_number'];
        $episodeTitle = $episode['name'];

        $formattedName = sprintf(
            "%s - S%02dE%02d - %s",
            $seriesName,
            $seasonNumber,
            $episodeNumber,
            $episodeTitle
        );

        $episodes[$episodeNumber] = $formattedName;
    }

    return $episodes;
}

function isOrganizedFileName($filename, $title): bool
{
    $escapedTitle = preg_quote($title, '/');
    $pattern = '/^' . $escapedTitle . ' - S\d{2}E\d{2} - /';
    return preg_match($pattern, $filename) === 1;
}

function renameMediaFiles($path, $showName, $episodes)
{
    $path = rtrim($path, "/");
    $files = scandir($path);
    $segmentCounts = [];
    $tokenizedFilenames = [];
    $fileGroups = [];
    $supportedExtensions = ["mp4", "mkv", "srt", "ass"];

    foreach ($files as $filename) {
        $extension = pathinfo($filename, PATHINFO_EXTENSION);
        if (!in_array($extension, $supportedExtensions)) {
            continue;
        }
        if (isOrganizedFileName($filename, $showName)) {
            continue;
        }
        $nameWithoutExtension = getNameWithoutExtension($filename);
        $fileGroups[$nameWithoutExtension][] = ["filename" => $filename, "extension" => $extension];
        $tokens = preg_split('/[\s\[\]()_\-.Ee]+/', $nameWithoutExtension, -1, PREG_SPLIT_NO_EMPTY);
        $tokenizedFilenames[$nameWithoutExtension] = $tokens;
        foreach ($tokens as $token) {
            $segmentCounts[$token] = ($segmentCounts[$token] ?? 0) + 1;
        }
    }

    $totalFiles = count($fileGroups);
    foreach ($fileGroups as $baseName => $fileSet) {
        $numericPart = "0";
        if ($totalFiles < 3) {
            if (preg_match('/\b(\d{1,3})\b/', clearSeasonSegment($baseName), $matches)) {
                $numericPart = intval($matches[1]);
            }
        } else {
            $tokens = $tokenizedFilenames[$baseName] ?? [];
            $uniqueParts = array_filter($tokens, function ($token) use ($segmentCounts, $fileSet) {
                return $segmentCounts[$token] <= count($fileSet);
            });
            foreach ($uniqueParts as $token) {
                if (preg_match('/\d{1,3}/', clearSeasonSegment($token), $matches)) {
                    $numericPart = ltrim($matches[0], "0Ee");
                    $numericPart = $numericPart === "" ? 0 : intval($numericPart);
                    break;
                }
            }
        }

        foreach ($fileSet as $file) {
            $originalFile = $file["filename"];
            $extension = $file["extension"];
            $newFilename = $episodes[$numericPart] ?? $numericPart;
            if (in_array($extension, ['ass', 'srt'])) {
                if (preg_match('/\.(chs|cht|zh|en|jp|jpn|eng|other)\./', $originalFile, $matches)) {
                    $newFilename .= $matches[0] . $extension;
                } else {
                    $newFilename .= '.' . $extension;
                }
            } else {
                $newFilename .= '.' . $extension;
            }
            $newFilename = cleanPath(strip_tags($newFilename));
            $newFilename = str_replace('/', ' ', $newFilename);
            if (strcasecmp($originalFile, $newFilename) === 0) {
                continue;
            }
            safeRename($path . '/' . $originalFile, $path . '/' . $newFilename);
        }
    }
}

function getNameWithoutExtension($filename)
{
    $nameWithoutExtension = pathinfo($filename, PATHINFO_FILENAME);
    return pathinfo($nameWithoutExtension, PATHINFO_FILENAME);
}

function clearSeasonSegment($baseName): string
{
    return preg_replace('/\s*-?\s*S\d{2}E/', ' ', $baseName);
}

function cleanPath($path, $trim = true)
{
    $path = $trim ? trim($path) : $path;
    $path = trim($path, '\\/');
    $path = str_replace(array('../', '..\\'), '', $path);
    if ($path == '..') {
        $path = '';
    }
    return str_replace('\\', '/', $path);
}

function safeRename($origin, $new)
{
    echo "Renaming: $origin -> $new\n";
    if (!file_exists($new) && file_exists($origin)) {
        rename($origin, $new);
    }
}
