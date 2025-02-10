<?php

define('TMDB_API_KEY', $_ENV['TMDB_API_KEY']);
define('TMDB_LANGUAGE', $_ENV['TMDB_LANGUAGE'] ?? 'zh-CN');

use Symfony\Component\Cache\Adapter\FilesystemAdapter;
use Tmdb\Client;
use Tmdb\Event\BeforeRequestEvent;
use Tmdb\Event\Listener\Psr6CachedRequestListener;
use Tmdb\Event\Listener\Request\AcceptJsonRequestListener;
use Tmdb\Event\Listener\Request\ApiTokenRequestListener;
use Tmdb\Event\Listener\Request\ContentTypeJsonRequestListener;
use Tmdb\Event\Listener\Request\LanguageFilterRequestListener;
use Tmdb\Event\Listener\Request\UserAgentRequestListener;
use Tmdb\Event\RequestEvent;
use Tmdb\Token\Api\ApiToken;
use Tmdb\Token\Api\BearerToken;

$token = defined('TMDB_BEARER_TOKEN') && TMDB_BEARER_TOKEN !== 'TMDB_BEARER_TOKEN' ?
    new BearerToken(TMDB_BEARER_TOKEN) :
    new ApiToken(TMDB_API_KEY);

try {
    $ed = new Symfony\Component\EventDispatcher\EventDispatcher();

    $client = new Client(
        [
            /** @var ApiToken|BearerToken */
            'api_token' => $token,
            'event_dispatcher' => [
                'adapter' => $ed
            ],
            // We make use of PSR-17 and PSR-18 auto discovery to automatically guess these, but preferably set these explicitly.
            'http' => [
                'client' => null,
                'request_factory' => null,
                'response_factory' => null,
                'stream_factory' => null,
                'uri_factory' => null,
            ]
        ]
    );

    $cache = new FilesystemAdapter('php-tmdb', 86400, __DIR__ . '/var/cache');
    $requestListener = new Psr6CachedRequestListener(
        $client->getHttpClient(),
        $ed,
        $cache,
        $client->getHttpClient()->getPsr17StreamFactory(),
        []
    );
    $ed->addListener(RequestEvent::class, $requestListener);
    $apiTokenListener = new ApiTokenRequestListener($client->getToken());
    $ed->addListener(BeforeRequestEvent::class, $apiTokenListener);
    $acceptJsonListener = new AcceptJsonRequestListener();
    $ed->addListener(BeforeRequestEvent::class, $acceptJsonListener);
    $jsonContentTypeListener = new ContentTypeJsonRequestListener();
    $ed->addListener(BeforeRequestEvent::class, $jsonContentTypeListener);
    $userAgentListener = new UserAgentRequestListener();
    $ed->addListener(BeforeRequestEvent::class, $userAgentListener);
    $ed->addListener(
        BeforeRequestEvent::class,
        new LanguageFilterRequestListener(TMDB_LANGUAGE)
    );
    return $client;
} catch (Exception $e) {
    echo "❌ Error: " . $e->getMessage() . "\n";
}
