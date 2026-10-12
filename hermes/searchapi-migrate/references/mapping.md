# SERP API to SearchApi mapping

Read SearchApi basics, then only the section for the user's vendor. The SearchApi side is checked against `https://www.searchapi.io/openapi/ENGINE.yaml`, the vendor side against its public docs and SDK source, all on 2026-10-12. "Docs only" marks a SearchApi engine or param that its docs page lists but its OpenAPI file lacks. If a row and the live params disagree, the live `searchapi.py params ENGINE PARAM` wins.

## Contents

- SearchApi basics
- SerpApi
- Serper
- DataForSEO
- Bright Data

## SearchApi basics

| Topic | SearchApi |
|---|---|
| Call | `GET https://www.searchapi.io/api/v1/search`, `engine` plus params as a query string, one search per call |
| Auth | `Authorization: Bearer KEY` header, key from `SEARCHAPI_API_KEY`; never in the URL |
| Success | HTTP 200, no top-level `error`, `search_metadata.status` is `Success` |
| Failure | top-level `error` string; 400 bad param, 401 key, 402 or 403 plan, 429 rate limit, 5xx retry (failed calls are not billed) |
| Pages | `page` (1-based), about 10 rows per page on web engines; `position` restarts at 1 on each page |
| Top 100 | `google_rank_tracking`, one call |
| Saved search | `search_metadata.json_url` re-reads a finished search |
| Unknown params | dropped without an error: compare what you sent with the echoed `search_parameters` |
| Locations | `location` wants a canonical `City,Region,Country`: a short name resolves to the most popular match, no match is a 400. Look names up with the searchapi skill's free `searchapi.py locations QUERY` |
| LangChain | `SearchApiAPIWrapper` (`langchain_community.utilities`) sends the Bearer header and reads `SEARCHAPI_API_KEY`; set its `engine` |

Main engines: `google`, `google_maps`, `google_maps_place`, `google_shopping`, `google_product`, `google_news`, `google_images`, `google_videos`, `google_scholar`, `youtube`, `bing`.

## SerpApi

Docs: serpapi.com/search-api, each engine page and the `serpapi` Python package (checked 2026-10-12). SerpApi and SearchApi look alike; most names carry over, so check the rows below for the ones that do not.

Auth and call

| SerpApi | SearchApi |
|---|---|
| `GET https://serpapi.com/search` with `api_key` in the query | `GET https://www.searchapi.io/api/v1/search` with the Bearer header |
| `serpapi.search({...})`, `serpapi.Client(...).search({...})` or legacy `GoogleSearch({...}).get_dict()` | one HTTP GET with any HTTP library, then parse JSON |
| `next_page()`, `yield_pages()`, legacy `GoogleSearch(...).pagination()` | a loop over `page` that stops on an empty or repeated page |
| `serpapi.SerpApiError` and its subclasses (`HTTPError`, `TimeoutError`); legacy `SerpApiClientException` or an `error` key in the dict | HTTP status plus top-level `error` |
| `search_metadata.json_endpoint`, `/searches/{id}.json` | `search_metadata.json_url` |
| `serpapi.account()`, `/account.json` | the searchapi skill's `credits` command |
| `serpapi.locations()`, `/locations.json` | the searchapi skill's `locations QUERY` command (free) |
| LangChain `SerpAPIWrapper` | `SearchApiAPIWrapper` (see basics) |
| `no_cache`, `async`, `json_restrictor`, `output`, `zero_trace` | no equivalent: drop them |

Engines

| SerpApi | SearchApi |
|---|---|
| `google` | `google` |
| `google` with `tbm=isch`, `nws`, `shop`, `vid` | `google_images`, `google_news`, `google_shopping`, `google_videos` |
| `google_news` (the news.google.com portal) | google_news_portal, docs only: see the note below |
| `google_maps` (`type=search`) | `google_maps` |
| `google_maps` with `place_id` (no `type` needed) | `google_maps_place` |
| the same name | `google_light`, `google_shopping`, `google_images`, `google_videos`, `google_scholar`, `google_news_light`, `google_jobs`, `google_finance`, `google_related_questions`, `google_ai_overview`, `youtube`, `bing` |
| the same name, with renamed params below | `google_maps_reviews`, `google_trends`, `google_lens`, `google_ai_mode`, `google_flights`, `google_hotels`, `youtube_video`, `yandex`, `yahoo`, `baidu`, `duckduckgo` |
| `amazon`, `walmart`, `ebay`, `google_play` | `amazon_search`, `walmart_search`, `ebay_search`, `google_play_store` |

SerpApi `google_news` is the news.google.com portal, not the Search news tab. Port it to `google_news_portal` (docs only, no OpenAPI file: https://www.searchapi.io/docs/google-news-portal-api). `q`, `topic_token`, `section_token`, `story_token` and `publication_token` keep their names (SearchApi takes `section_token` only with `topic_token`); `gl` plus `hl` become `ceid` (`US:en`); `so=1` becomes `sort_by=date` (with `story_token` only); `kgmid` has no equivalent. Its `news_results[]` become flat `organic_results[]`: `source.icon` is `source.favicon`, `date` is `iso_date`, and story clusters arrive as rows with `story_title` and `story_token`.

Params

| SerpApi | SearchApi | SearchApi engines | Notes |
|---|---|---|---|
| `q` | `q` | google, google_light, google_maps, google_shopping, google_news, google_news_light, google_images, google_videos, google_scholar, bing | |
| `search_query` | `q` | youtube | |
| `location`, `uule` | `location`, `uule` | google, google_light, google_shopping, google_news, google_news_light, google_images, google_videos, google_jobs, google_ai_mode | canonical `City,Region,Country`: check it with `locations QUERY` |
| `location` | `location` | bing | |
| `lat`, `lon` | `latitude`, `longitude` | google | `radius` has no equivalent |
| `lat`, `lon` | `lat`, `lon` | bing | |
| `location`, `lat`, `lon`, `z`, `m` | `ll` | google_maps | build `@LAT,LON,14z` (zoom) or `@LAT,LON,500m` (metres); a `location` name goes in `q` instead |
| `ll` | `ll` | google_maps | same `@lat,lng,14z` form, or metres: `@43.85,11.21,10410m` |
| `gl`, `hl` | `gl`, `hl` | google, google_light, google_maps, google_shopping, google_news, google_news_light, google_images, google_videos, google_jobs, google_ai_mode, youtube | |
| `cr`, `lr`, `nfpr`, `filter` | `cr`, `lr`, `nfpr`, `filter` | google, google_news, google_images, google_videos | |
| `safe` | `safe` | google, google_light, google_images, google_videos | google_shopping and google_scholar: no equivalent |
| `device` | `device` | google, google_news, google_images, google_videos, bing | bing takes `desktop` or `mobile` only; google_shopping: no equivalent |
| `kgmid` | `kgmid` | google | |
| `tbs` date (`qdr:h` to `qdr:y`) | `time_period` | google, google_news, google_news_light, google_images, google_videos | `last_hour` to `last_year` |
| `tbs` custom range (`cdr:1,cd_min:...,cd_max:...`) | `time_period_min`, `time_period_max` | google, google_news, google_videos | `MM/DD/YYYY`; google_videos: OpenAPI only; google_images: no equivalent |
| `when:` inside `q` | `time_period` | google_news | only when portal code moves to the Search news tab |
| `start` | `page` | google, google_light, google_news, google_news_light, google_videos, google_scholar | `page = start / 10 + 1` (scholar: `start / num + 1`) |
| `start` (steps of 20) | `page` | google_maps | `page = start / 20 + 1` |
| `ijn` | `page` | google_images | `page = ijn + 1` |
| `first` | `page` | bing | `page = (first - 1) / 10 + 1` |
| `num` (google), `count` (bing) | none | google, bing | both legacy and ignored: loop `page`, or `google_rank_tracking` for a top 100 |
| `num` | `num` | google_scholar | 1 to 20 |
| `type` | none | google_maps | drop it: `google_maps` searches, `google_maps_place` reads one place |
| `place_id` | `place_id` | google_maps_place | `data_cid` has no equivalent: use `place_id` |
| `type=place` with the old `data` param | `data_id` | google_maps_place | pass the `0x...:0x...` part after `!1s` |
| `nearby`, `min_rating`, `open_state`, `open_on_day`, `open_at_hour`, `min_price`, `max_price` | none | google_maps | filter the rows on `rating`, `open_hours` and `price` yourself |
| `google_domain` | none | | use `gl` |
| `shoprs` | `shoprs` | google_shopping | |
| `min_price`, `max_price` | `price_min`, `price_max` | google_shopping | |
| `sort_by` | `sort_by` | google_shopping | `1` becomes `price_low_to_high`, `2` becomes `price_high_to_low` |
| `on_sale`, `small_business`, `free_shipping` | docs only | google_shopping | `is_on_sale`, `is_small_business`, `is_free_delivery`, each `true`; ignored when `shoprs` is set |
| `start`, `num` | none | google_shopping | SerpApi ignores both; page with `page` |
| `period_unit`, `period_value` | `time_period` | google_images | units `h`, `d`, `w`, `m`, `y` with value 1 only; `s`, `n`, other values, `start_date` and `end_date` have no equivalent |
| `imgsz`, `image_color`, `imgar`, `licenses` | `size`, `color`, `aspect_ratio`, `usage_rights` | google_images | values differ: `params google_images PARAM` |
| `image_type` | `image_type` | google_images | |
| `cites`, `cluster`, `as_ylo`, `as_yhi`, `scisbd`, `as_sdt`, `as_vis`, `as_rr`, `lr` | `cites`, `cluster`, `as_ylo`, `as_yhi`, `scisbd`, `as_sdt`, `as_vis`, `as_rr`, `lr` | google_scholar | |
| `sp` | `sp` | youtube | the next page token also goes in `sp` |
| `mkt`, `cc` | `market_code`, `country_code` | bing | `market_code` is lowercase (`en-us`), `country_code` uppercase (`US`) |
| `safeSearch` | `safe_search` | bing | `Off`, `Moderate`, `Strict` become `off`, `moderate`, `strict` |
| `filters` | `filters` | bing | |
| `filters` date range (`ex1:...`) | `time_period`, `time_period_min`, `time_period_max` | bing | `time_period` starts at `last_day`; never with `filters` |
| news and video `link`: a publisher URL | `link` | google_news, google_videos | send `link=resolved`: the default can be a Google redirect URL |
| `lr`, `device`, `safe` | none | google_news_light | port to `google_news` when the code needs `lr` or `device`; `safe` has no equivalent |
| `chips`, `lrad`, `ltype`, `uds` | none | google_jobs | filter the rows yourself |
| `sort_by`, `query` | `sort_by` | google_maps_reviews | `qualityScore`, `newestFirst`, `ratingHigh`, `ratingLow` become `most_relevant`, `newest`, `highest_rating`, `lowest_rating`; `query` has no equivalent |
| `date` | `time` | google_trends | same values (`today 12-m`) |
| `type`, `safe` | `search_type`, `safe_search` | google_lens | |
| `subsequent_request_token`, `image_url` | `conversation_token`, `url` | google_ai_mode | drop `continuable`: every response carries `pagination.conversation_token` |
| `type`, `travel_class`, `stops`, `bags`, `show_hidden`, `exclude_basic`, `exclude_airlines`, `include_airlines`, `exclude_conns`, `max_duration`, `layover_duration` | `flight_type`, `travel_class`, `stops`, `carry_on_bags`, `show_hidden_flights`, `exclude_basic_economy`, `excluded_airlines`, `included_airlines`, `excluded_connecting_airports`, `max_flight_duration`, `layover_duration_min`, `layover_duration_max` | google_flights | numbers become words (`type=2` is `one_way`, `travel_class=3` is `business`, `stops=1` is `nonstop`); split `layover_duration=90,330` |
| `min_price`, `max_price`, `vacation_rentals` | `price_min`, `price_max`, `property_type` | google_hotels | `vacation_rentals=true` is `property_type=vacation_rental`; `sort_by` `3`, `8`, `13` become `lowest_price`, `highest_rating`, `most_reviewed` |
| `v` | `video_id` | youtube_video | comments: `youtube_comments` |
| `k`, `s` | `q`, `sort_by` | amazon_search | sort values differ: `params amazon_search sort_by` |
| `query`, `sort`, `min_price`, `max_price` | `q`, `sort_by`, `price_min`, `price_max` | walmart_search | |
| `_nkw`, `_pgn`, `_ipg`, `_udlo`, `_udhi`, `_sop` | `q`, `page`, `num`, `price_min`, `price_max`, `sort_by` | ebay_search | `num` takes 60, 120 or 240; sort values differ |
| `apps_category` | `category` | google_play_store | |
| `text`, `p`, `lr`, `period` | `q`, `page`, `location`, `time_period` | yandex | `page = p + 1`; `lr` region IDs carry over; `day`, `month` become `last_day`, `last_month` |
| `p`, `b` | `q`, `page` | yahoo | `page = (b - 1) / 10 + 1` |
| `pn`, `rn` | `page`, `num` | baidu | `page = pn / 10 + 1` |
| `kl`, `df`, `start` | `locale`, `time_period`, `next_page_token` | duckduckgo | `d`, `w`, `m`, `y` become `past_day` to `past_year`; date ranges carry over; page with `pagination.next_page_token` |

Response fields

| SerpApi | SearchApi |
|---|---|
| `organic_results[]` (google, bing, scholar) | `organic_results[]`, same row names; `position` restarts at 1 per page |
| `serpapi_pagination` (serpapi.com links), `pagination` (google.com links) | `pagination` (`current`; `next` is a google.com link): ask for `page + 1` |
| google `local_results.places[]`: `place_id` is the numeric CID | `local_results[]` (an array): the CID is `ludocid` |
| google_maps `local_results[]`: `place_id`, `data_id`, `data_cid`, `gps_coordinates`, `rating`, `reviews`, `address`, `phone`, `website`, `operating_hours` | `local_results[]`: same names, but `data_cid` becomes `ludocid` and `operating_hours` becomes `open_hours` |
| google_maps `place_results` (one object) | google_maps_place `place_result` (one object) |
| `shopping_results[].source` | `shopping_results[].seller`; `immersive_product_page_token` becomes `product_token` for `google_product` |
| `tbm=nws` `news_results[]`: `source` (a string) | google_news `organic_results[]`: `source` (a string) |
| portal `news_results[]`: `source.name` | google_news_portal `organic_results[]`: `source.name` (see the note above) |
| `images_results[]`: `original`, `link`, `source`, `original_width` | `images[]`: `original.link`, `source.link`, `source.name`, `original.width` |
| scholar `result_id`, `publication_info.summary`, `publication_info.authors` | `data_cid`, `publication`, `authors`; `inline_links.cited_by` is the same |
| `video_results[]`: `position_on_page`, `video_id`, `published_date`, `channel.name` | `videos[]`: `position`, `id`, `published_time`, `channel.title` |
| `serpapi_pagination.next_page_token` (youtube) | `pagination.next_page_token`, sent back as `sp` |
| `serpapi_link` and other `serpapi_*` links | none: build the next call from params |

## Serper

Docs: serper.dev, its playground and public samples, plus the LangChain, CrewAI and Haystack clients (checked 2026-10-12; Serper publishes no standalone API reference). Serper takes one JSON body per endpoint and returns camelCase fields; SearchApi takes `engine` plus params and returns snake_case.

Auth and call

| Serper | SearchApi |
|---|---|
| `POST https://google.serper.dev/search` (or another endpoint below) with a JSON body, or the same params in the query string | `GET https://www.searchapi.io/api/v1/search` with `engine` and params as a query string |
| `X-API-KEY` header, or `apiKey` in a GET query | Bearer header; never in the URL |
| LangChain `GoogleSerperAPIWrapper` (it sends the params in the query string) | `SearchApiAPIWrapper` (see basics) |
| a JSON array body (mini-batch, up to 100 queries) | one GET per query; many queries: the searchapi skill's `batch` |
| error body `message` | top-level `error` |
| `searchParameters` | `search_parameters` |
| `credits` in each response, GET `/account` | the searchapi skill's `credits` command |
| `scrape.serper.dev` (page scraper) | no equivalent |

Engines

| Serper | SearchApi |
|---|---|
| `/search` | `google` |
| `/images` | `google_images` |
| `/videos` | `google_videos` (YouTube itself: `youtube`) |
| `/places`, `/maps` | `google_maps` |
| `/maps` with `placeId` or `cid` | `google_maps_place` |
| `/reviews` | `google_maps_reviews` |
| `/news` | `google_news` |
| `/shopping` | `google_shopping` |
| `/scholar` | `google_scholar` |
| `/patents` | `google_patents` |
| `/autocomplete` | `google_autocomplete` |
| `/lens` (image `url`) | `google_lens` |
| Bing (not public on Serper) | `bing` |

Params

| Serper | SearchApi | SearchApi engines | Notes |
|---|---|---|---|
| `q` | `q` | google, google_images, google_videos, google_maps, google_news, google_shopping, google_scholar, google_patents, google_autocomplete | |
| `gl` | `gl` | google, google_images, google_videos, google_maps, google_news, google_shopping, google_autocomplete | |
| `hl` | `hl` | google, google_images, google_videos, google_maps, google_news, google_shopping, google_scholar, google_autocomplete | |
| `location` | `location` | google, google_images, google_videos, google_news, google_shopping | canonical `City,Region,Country`: check it with `locations QUERY` |
| `location` on `/places` | `q` | google_maps | `google_maps` has no `location`: put the place in `q`, or use `ll` |
| `page` | `page` | google, google_images, google_videos, google_maps, google_news, google_shopping, google_scholar, google_patents | same 1-based pages |
| `num` | none | google, google_images | `/search` now returns about 10, `/images` 10 or 100: loop `page`, or `google_rank_tracking` for a web top 100 |
| `num` | `num` | google_shopping | OpenAPI only (default 60) |
| `tbs` (`qdr:h` to `qdr:y`) | `time_period` | google, google_images, google_videos, google_news | `last_hour` to `last_year` |
| `autocorrect: false` | `nfpr` | google, google_images, google_videos, google_news | `nfpr=1` |
| `ll` | `ll` | google_maps | same `@lat,lng,zoom` form |
| `placeId`, `fid` | `place_id`, `data_id` | google_maps_place, google_maps_reviews | `cid` alone has no equivalent: keep the `placeId` or `fid` from the `/maps` rows |
| news and video `link`: a publisher URL | `link` | google_news, google_videos | send `link=resolved`: the default can be a Google redirect URL |

Response fields

| Serper | SearchApi |
|---|---|
| `organic[]`: `title`, `link`, `snippet`, `date`, `position` | `organic_results[]`: same names |
| `knowledgeGraph`, `answerBox`, `topStories` | `knowledge_graph`, `answer_box`, `top_stories` |
| `peopleAlsoAsk[]`: `question`, `snippet`, `link` | `related_questions[]`: `question`, `answer`, `source.link` |
| `relatedSearches[]`: `query` | `related_searches[]`: `query` |
| `/places` `places[]`: `title`, `address`, `latitude`, `longitude`, `rating`, `ratingCount`, `category`, `phoneNumber`, `website`, `cid` | `local_results[]`: `title`, `address`, `gps_coordinates.latitude`, `gps_coordinates.longitude`, `rating`, `reviews`, `type`, `phone`, `website`, `ludocid` |
| `/maps` `places[]`: the `/places` names without `category`, plus `type`, `types`, `description`, `fid`, `placeId`, `openingHours`, `thumbnailUrl`, `priceLevel` | `local_results[]`: as above, plus `type`, `types`, `description`, `data_id`, `place_id`, `open_hours`, `thumbnail`, `price` |
| `/maps` with `placeId` or `cid`: `places[0]` (an unknown id gives HTTP 200 and `places: []`) | google_maps_place `place_result` (one object) |
| `shopping[]`: `source`, `price`, `imageUrl`, `ratingCount`, `productId`, `link` | `shopping_results[]`: `seller`, `price` (`extracted_price` is a number), `thumbnail`, `reviews`, `product_id`, `product_link` |
| `news[]`: `source`, `date`, `imageUrl` | `organic_results[]`: `source`, `date`, `iso_date`, `thumbnail` |
| `images[]`: `imageUrl`, `imageWidth`, `imageHeight`, `thumbnailUrl`, `link`, `source` | `images[]`: `original.link`, `original.width`, `original.height`, `thumbnail`, `source.link`, `source.name` |
| scholar `organic[]`: `publicationInfo`, `citedBy`, `year` | `organic_results[]`: `publication`, `inline_links.cited_by.total` (the year is inside `publication`) |
| `videos[]`: `imageUrl`, `duration`, `channel`, `source` | `videos[]`: `thumbnail`, `length`, `channel`, `source` |

## DataForSEO

Docs: docs.dataforseo.com/v3/serp and /v3/merchant (checked 2026-10-12). DataForSEO sends a task array by POST and returns one mixed `items[]` list; SearchApi is one GET per query with one array per result block.

Auth and call

| DataForSEO | SearchApi |
|---|---|
| `POST https://api.dataforseo.com/v3/serp/google/organic/live/advanced` (and the other `live/advanced` paths) with a JSON array holding one task: live paths take one task per call | one GET per query; many queries: the searchapi skill's `batch` |
| `task_post` (up to 100 tasks per call), `tasks_ready`, `task_get/advanced/{id}`, `postback_url`, `pingback_url`, `priority` | none: calls are synchronous; each task becomes one GET |
| HTTP Basic `login:password` (`RestClient`, `dataforseo-client`) | Bearer key |
| LangChain `DataForSeoAPIWrapper` | `SearchApiAPIWrapper` (see basics) |
| `status_code` `20000` and `tasks[].status_code` | HTTP status plus top-level `error` |
| `tag` | none: keep your own query id |
| `check_url` | `search_metadata.request_url` |

Engines

| DataForSEO | SearchApi |
|---|---|
| `/v3/serp/google/organic/live/advanced` | `google` (`google_rank_tracking` for depth 100) |
| `/v3/serp/google/maps/live/advanced`, `/v3/serp/google/local_finder/live/advanced` | `google_maps` |
| `/v3/merchant/google/products/task_post` | `google_shopping` |
| `/v3/serp/google/news/live/advanced` | `google_news` |
| `/v3/serp/google/images/live/advanced` | `google_images` |
| `/v3/serp/google/ai_mode/live/advanced` | `google_ai_mode` |
| `/v3/serp/google/autocomplete/live/advanced` | `google_autocomplete` |
| `/v3/serp/google/jobs/task_post` | `google_jobs` |
| `scholarly_articles` items on an organic SERP (no Scholar endpoint) | `google` (`scholarly_articles.articles[]`); a real Scholar search: `google_scholar` |
| `/v3/serp/youtube/organic/live/advanced` | `youtube` |
| `/v3/serp/youtube/video_info`, `video_subtitles`, `video_comments` | `youtube_video`, `youtube_transcripts`, `youtube_comments` |
| `/v3/serp/bing/organic/live/advanced` | `bing` |
| any `task_post` path, read back with `task_get/advanced/{id}` | the engine of its `live/advanced` twin |

Params

| DataForSEO | SearchApi | SearchApi engines | Notes |
|---|---|---|---|
| `keyword` | `q` | google, google_maps, google_shopping, google_news, google_images, google_ai_mode, google_autocomplete, google_jobs, youtube, bing | |
| `location_name` | `location` | google, google_shopping, google_news, google_images, google_ai_mode, google_jobs, bing | names such as `London,England,United Kingdom` carry over |
| `location_code` | `location` | google, google_shopping, google_news, google_images, google_ai_mode, google_jobs, bing | turn the code into its name once, then confirm it with `locations QUERY` |
| `language_code`, `language_name` | `hl` | google, google_maps, google_shopping, google_news, google_images, google_ai_mode, google_autocomplete, google_jobs, youtube | codes only (`en`, not `English`); add `gl` for the country |
| `language_code` | `language` | bing | |
| `location_coordinate` | `latitude`, `longitude` | google | the radius part has no equivalent |
| `location_coordinate` | `ll` | google_maps | `52.61,-1.35,17z` becomes `@52.61,-1.35,17z`; append `,17z` when the zoom is missing (DataForSEO's default) |
| `location_coordinate` | `lat`, `lon` | bing | |
| `device` | `device` | google, bing | `desktop` or `mobile`; news and images have no `device`; maps and YouTube `device`, and `os`, have no equivalent |
| `depth` | `page` | google, google_news, bing | default 10 (max 200 live, 700 queued): loop `page` to depth / 10; top 100: `google_rank_tracking` |
| `depth` | `page` | google_images | default 100: loop `page` |
| `depth` | `page` | google_maps | default 100 (max 700): loop about 5 pages of 20 rows |
| `max_crawl_pages` | `page` | google, google_news, google_images, bing | the number of `page` calls (max 100, each one billed) |
| `block_depth` | `sp` | youtube | pass `pagination.next_page_token` as `sp` |
| `price_min`, `price_max`, `sort_by` | `price_min`, `price_max`, `sort_by` | google_shopping | same values (`review_score`, `price_low_to_high`, `price_high_to_low`) |
| `search_param` | the named param | | values start with `&`: `&tbs=qdr:d` becomes `time_period=last_day`, `&start=20` becomes `page=3`; YouTube puts `sp=` here |
| `cursor_pointer` | `cp` | google_autocomplete | |
| `se_domain`, `os`, `target`, `stop_crawl_on_match`, `calculate_rectangles`, `people_also_ask_click_depth`, `url`, `group_organic_results`, `search_this_area`, `search_places` | none | | use `gl`; filter rows by `domain` yourself |
| `load_async_ai_overview` | none needed | | `google` returns `ai_overview` inline or with a `page_token` for `google_ai_overview` |
| news `url`: a publisher URL | `link` | google_news | send `link=resolved`: the default can be a Google redirect URL |

Response fields

| DataForSEO | SearchApi |
|---|---|
| `tasks[0].result[0].items[]` by `type` | one array per block: `organic_results`, `ads`, `related_questions`, `answer_box`, `local_results`, `top_stories`, `ai_overview`, `knowledge_graph` |
| `rank_group` | `position` plus the rows of earlier pages (pages often hold under 10) |
| `rank_absolute` | none: positions count within each block |
| `url`, `description`, `breadcrumb`, `domain` | `link`, `snippet`, `displayed_link`, `domain` |
| `timestamp` | `date` (web), `iso_date` (news) |
| `rating.value`, `rating.votes_count` | `rating`, `reviews` |
| maps `cid`, `feature_id`, `place_id`, `latitude`, `longitude` | `ludocid`, `data_id`, `place_id`, `gps_coordinates.latitude`, `gps_coordinates.longitude` |
| maps `category`, `additional_categories`, `work_hours.timetable`, `work_hours.current_status`, `main_image` | `type`, `types`, `open_hours` (text per day), `open_state`, `thumbnail` |
| shopping `shopping_url`, `price` (a number, plus `currency`), `old_price`, `product_rating.value` (a string), `reviews_count` | `product_link`, `extracted_price` (the currency symbol is in `price`), `extracted_original_price`, `rating` (a number), `reviews` |
| `scholarly_articles` `items[]`: `title`, `url`, `author`, `description` (cited count) | `scholarly_articles.articles[]`: `title`, `link`, `author`, `cited_by` |
| news `url`, `time_published`, `timestamp` | `link`, `date`, `iso_date` |
| images `url` (page), `source_url` (image) | `source.link`, `original.link` |
| youtube `video_id`, `channel_name`, `views_count`, `duration_time`, `publication_date`, `thumbnail_url` | `id`, `channel.title`, `views`, `length`, `published_time`, `thumbnail.static` |

## Bright Data

Docs: docs.brightdata.com/products/serp-api and the `brightdata` Python SDK (checked 2026-10-12). Bright Data takes the engine's own URL and returns parsed JSON on request; SearchApi takes `engine` plus params and always returns JSON. Bright Data documents no Scholar or YouTube search in its SERP API.

Auth and call

| Bright Data | SearchApi |
|---|---|
| `POST https://api.brightdata.com/request` with `zone`, `url`, `format` and a Bearer token | `GET https://www.searchapi.io/api/v1/search` with `engine`, params and the Bearer header |
| proxy `brd.superproxy.io` with zone user, password and certificate | the same GET: no proxy, zone or certificate |
| SDK `BrightDataClient` (`client.search.google(...)`), `bdclient`, `langchain_brightdata` | one HTTP GET, or `SearchApiAPIWrapper` (see basics) |
| `brd_json=1` in the URL, or body `data_format` (`parsed_light`, `parsed_bing`, `parsed_bing_api`), or `x-unblock-data-format` | none: always JSON (`parsed_light` kept only the top 10 organic rows) |
| `"format": "json"`: wraps the page in `status_code`, `headers`, `body` | none: the response is the result JSON |
| `brd_json=html`; `data_format` `markdown` or `screenshot` | `search_metadata.html_url` (the captured HTML; no markdown or screenshot) |
| `?async=true`, `x-response-id`, `get_result`, `webhook_url` | none: calls are synchronous |
| HTTP 200 that hides a failure: the real status is in `x-brd-status-code`, the code in `x-brd-error-code` (or `x-brd-err-code`), the message in `x-brd-error` | HTTP status plus top-level `error`: a failed search is never a 200 |
| top 100 dataset `gd_mfz5x93lmsjjjylob` (`start_page`, `end_page` snapshots) | `google_rank_tracking`, one call |

Engines

| Bright Data target URL | SearchApi |
|---|---|
| `https://www.google.com/search?q=...` | `google` |
| the same with `tbm=nws` | `google_news` |
| the same with `udm=2` (or `tbm=isch`, retired in March 2026) | `google_images` |
| the same with `udm=28` or `tbm=shop` | `google_shopping` |
| the same with `udm=7` or `tbm=vid` | `google_videos` |
| the same with `udm=50` (AI Mode) | `google_ai_mode` |
| the same with `ibp=htl;jobs` | `google_jobs` |
| `https://www.google.com/maps/search/QUERY/@lat,lon,14z` | `google_maps` |
| `/maps/place/data=!3m1!4b1!4m2!3m1!1sFID`, `/maps/place/?q=place_id:ID` | `google_maps_place` |
| `https://www.google.com/reviews?fid=FID` (failing on Bright Data since 2026-07-22) | `google_maps_reviews` |
| `https://www.google.com/travel/hotels/...` | `google_hotels` (one hotel's prices: `google_hotels_property`) |
| `https://www.google.com/travel/flights/search?tfs=...` (the encoded `tfs` has no equivalent) | `google_flights`: rebuild the search from its params |
| `https://trends.google.com/trending` | `google_trends_trending_now` |
| `https://lens.google.com/uploadbyurl?url=...` | `google_lens` |
| `https://www.bing.com/search?q=...`, or body `search_engine: "bing"` with `query` | `bing` |

Params

| Bright Data | SearchApi | SearchApi engines | Notes |
|---|---|---|---|
| `q` | `q` | google, google_news, google_images, google_shopping, google_videos, google_ai_mode, google_jobs, bing | |
| body `query` (with `search_engine`) | `q` | bing | |
| query in the Maps path | `q` | google_maps | |
| `@lat,lon,14z` in the Maps path | `ll` | google_maps | |
| `place_id:ID` | `place_id` | google_maps_place | |
| `fid` (place URL or reviews) | `data_id` | google_maps_place, google_maps_reviews | the `0x...:0x...` value |
| `gl`, `hl` | `gl`, `hl` | google, google_maps, google_news, google_images, google_shopping, google_videos | body `country` only picks the proxy exit country: set `gl` for the results |
| `uule`, encoded (`w+CAIQ...`) | `uule` | google, google_news, google_images, google_shopping, google_videos | |
| `uule`, a plain name (`New+York,New+York,United+States`) | `location` | google, google_news, google_images, google_shopping, google_videos | `New York,New York,United States`: check it with `locations QUERY` |
| `uule`, `lat,lon,radius` | `latitude`, `longitude` | google | the radius has no equivalent |
| `start` | `page` | google, google_news, google_images, google_shopping, google_videos | `page = start / 10 + 1` |
| `num` | none | google | deprecated by Google on 2025-09-11: loop `page` |
| `tbs=qdr:d` and so on | `time_period` | google, google_news, google_images, google_videos | `last_hour` to `last_year` |
| `safe` | `safe` | google, google_images, google_videos | |
| `brd_mobile` | `device` | google, google_news, google_images, google_videos, bing | `1`, `ios`, `iphone`, `android` become `mobile`; `ipad`, `ios_tablet`, `android_tablet` become `tablet` (not on bing) |
| `cc`, `setLang` | `country_code`, `language` | bing | `country_code` is uppercase (`US`); `language` takes `en`, `en-gb` and so on |
| `mkt` | `market_code` | bing | lowercase values: `en-us` |
| `first` | `page` | bing | `page = (first - 1) / 10 + 1` |
| `location`, `lat`, `lon` | `location`, `lat`, `lon` | bing | |
| `safesearch` | `safe_search` | bing | |
| `filters` | `filters` | bing | or `time_period` (from `last_day`), never both |
| `brd_dates`, `brd_occupancy`, `brd_free_cancellation` | `check_in_date`, `check_out_date`, `adults`, `children_ages`, `free_cancellation` | google_hotels | `brd_occupancy=2,5,7` becomes `adults=2`, `children_ages=5,7` |
| trends `geo`, `hours` | `geo`, `time` | google_trends_trending_now | `hours` 4, 24, 48, 168 become `past_4_hours`, `past_24_hours`, `past_48_hours`, `past_7_days` |
| `count`, `brd_browser`, `brd_accommodation_type` | none | | |
| `brd_ai_overview` | none needed | | `google` returns `ai_overview` inline or with a `page_token` for `google_ai_overview` |
| news and video `link` | `link` | google_news, google_videos | send `link=resolved` for publisher URLs: the default can be a Google redirect URL |

Response fields

| Bright Data | SearchApi |
|---|---|
| `organic[]`: `link`, `title`, `display_link`, `description`, `icon`, `rank` | `organic_results[]`: `link`, `title`, `displayed_link`, `snippet`, `favicon`, `position` |
| `global_rank` | none: `position` counts within each block, restarting each page |
| `general.results_cnt`, `input.original_url` | `search_information.total_results`, `search_metadata.request_url` |
| `pagination.next_page_start` | `pagination` (ask for `page + 1`) |
| `top_ads`, `bottom_ads` | `ads` (`block_position` says top or bottom) |
| `people_also_ask[]`: `question`, `answers[].value.text` (older shape: `answer`), `answer_link` (older: `link`) | `related_questions[]`: `question`, `answer`, `source.link` |
| `related[]`: `text`, `link` | `related_searches[]`: `query`, `link` |
| `featured_snippets`, `knowledge`, `snack_pack` | `answer_box`, `knowledge_graph`, `local_results` |
| `snack_pack[]`: `cid`, `name`, `reviews_cnt` | `local_results[]`: `ludocid`, `title`, `reviews` |
| `ai_overview.texts`, `ai_overview.references[].href` | `ai_overview.markdown`, `ai_overview.reference_links[].link` |
| news `news[]`: `source`, `description`, `date` | `organic_results[]`: `source`, `snippet`, `date`, `iso_date` |
| images `images[]`: `link`, `original_image`, `image` | `images[]`: `source.link`, `original.link`, `thumbnail` |
| shopping `shopping[]`: `shop`, `reviews_cnt`, `link` | `shopping_results[]`: `seller`, `reviews`, `product_link` |
| maps `organic[]` (a search) or `place` (one place): `reviews_cnt`, `latitude`, `longitude`, `category[0].title`, `category[].title`, `open_hours` | `local_results[]` or `place_result`: `reviews`, `gps_coordinates.latitude`, `gps_coordinates.longitude`, `type`, `types`, `open_hours` |
| Bing `parsed_bing` `organic[]`: `link`, `title`, `description`, `display_link`, `rank` | `organic_results[]`: `link`, `title`, `snippet`, `displayed_link`, `position` |
| Bing `parsed_bing_api` `webPages.value[]`: `name`, `url`, `snippet` | `organic_results[]`: `title`, `link`, `snippet` |
