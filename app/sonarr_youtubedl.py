import requests
import urllib.parse
import yt_dlp
import os
import sys
import re
from utils import upperescape, checkconfig, offsethandler, YoutubeDLLogger, ytdl_hooks, ytdl_hooks_debug, setup_logging, find_best_match_index  # NOQA
from datetime import datetime
import schedule
import time
import logging
import argparse
from flask import Flask, json, render_template, request, redirect, url_for
import yaml
import threading

# allow debug arg for verbose logging
parser = argparse.ArgumentParser(description='Process some integers.')
parser.add_argument('--debug', action='store_true', help='Enable debug logging')
args = parser.parse_args()

# setup logger
logger = setup_logging(True, True, args.debug)

date_format = "%Y-%m-%dT%H:%M:%SZ"
now = datetime.now()

CONFIGFILE = os.environ.get('CONFIGPATH', './config.yml')
CONFIGPATH = CONFIGFILE.replace('config.yml', '')
SCANINTERVAL = 60

app = Flask(__name__)

@app.route('/')
def index():
    if not os.path.exists(CONFIGFILE):
        default_config = {
            'sonarrytdl': {'scan_interval': 1, 'debug': False},
            'sonarr': {
                'host': '', 
                'port': 8989, 
                'apikey': '', 
                'ssl': False,
                'basedir': 'sonarr',
                'version': 'v4'
            },
            'ytdl': {'default_format': 'bestvideo[width<=1920]+bestaudio/best[width<=1920]', 'extra_args': {}},
            'series': []
        }
        with open(CONFIGFILE, 'w') as ymlfile:
            yaml.safe_dump(default_config, ymlfile)
    with open(CONFIGFILE, 'r') as ymlfile:
        cfg = yaml.safe_load(ymlfile)
    return render_template('index.html', config=cfg)

@app.route('/update', methods=['POST'])
def update_config():
    new_config = {
        'sonarrytdl': {
            'scan_interval': request.form.get('sonarrytdl[scan_interval]', type=int),
            'debug': request.form.get('sonarrytdl[debug]') == 'on'
        },
        'sonarr': {
            'host': request.form.get('sonarr[host]'),
            'port': request.form.get('sonarr[port]', type=int),
            'apikey': request.form.get('sonarr[apikey]'),
            'ssl': request.form.get('sonarr[ssl]') == 'on',
            'basedir': request.form.get('sonarr[basedir]'),
            'version': request.form.get('sonarr[version]')
        },
        'ytdl': {
            'default_format': request.form.get('ytdl[default_format]'),
            'extra_args': json.loads(request.form.get('ytdl[extra_args]', '{}'))
        },
        'series': []
    }

    series_count = len([key for key in request.form.keys() if key.startswith('series[') and key.endswith('][title]')])
    for i in range(series_count):
        series_item = {
            'title': request.form.get(f'series[{i}][title]'),
            'url': request.form.get(f'series[{i}][url]'),
            'offset': {
                'days': request.form.get(f'series[{i}][offset][days]', type=int),
                'hours': request.form.get(f'series[{i}][offset][hours]', type=int)
            },
            'regex': {
                'sonarr': {
                    'match': request.form.get(f'series[{i}][regex][sonarr][match]'),
                    'replace': request.form.get(f'series[{i}][regex][sonarr][replace]')
                }
            }
        }
        new_config['series'].append(series_item)

    with open(CONFIGFILE, 'w') as ymlfile:
        yaml.safe_dump(new_config, ymlfile)
    return redirect(url_for('index'))


class SonarrYTDL(object):

    def __init__(self):
        """Set up app with config file settings"""
        cfg = checkconfig()

        # Sonarr_YTDL Setup

        try:
            self.set_scan_interval(cfg['sonarrytdl']['scan_interval'])
            try:
                self.debug = cfg['sonarrytdl']['debug'] in ['true', 'True']
                if self.debug:
                    logger.setLevel(logging.DEBUG)
                    for logs in logger.handlers:
                        if logs.name == 'FileHandler':
                            logs.setLevel(logging.DEBUG)
                        if logs.name == 'StreamHandler':
                            logs.setLevel(logging.DEBUG)
                    logger.debug('DEBUGGING ENABLED')
            except AttributeError:
                self.debug = False
        except Exception:
            sys.exit("Error with sonarrytdl config.yml values.")

        # Sonarr Setup
        try:
            api = "api"
            scheme = "http"
            basedir = ""
            if cfg['sonarr'].get('version', '').lower() == 'v4':
                api = "api/v3"
                logger.debug('Sonarr api set to v4')
            if cfg['sonarr']['ssl'].lower() == 'true':
                scheme = "https"
            if cfg['sonarr'].get('basedir', ''):
                basedir = '/' + cfg['sonarr'].get('basedir', '')

            self.base_url = "{0}://{1}:{2}{3}".format(
                scheme,
                cfg['sonarr']['host'],
                str(cfg['sonarr']['port']),
                basedir
            )
            self.sonarr_api_version = api
            self.api_key = cfg['sonarr']['apikey']
        except Exception as e:
            sys.exit("Error with sonarr config.yml values: {e}")

        # YTDL Setup
        try:
            self.ytdl_format = cfg['ytdl']['default_format']

            self.ytdl_subtitles = False
            if 'subtitles' in cfg['ytdl'].keys():
                self.ytdl_subtitles = True
                self.ytdl_subtitles_languages = ["en"]
                if 'languages' in cfg['ytdl']["subtitles"]:
                    self.ytdl_subtitles_languages = cfg['ytdl']["subtitles"]["languages"]
                self.ytdl_subtitles_autogenerated = False
                if 'autogenerated' in cfg['ytdl']["subtitles"]:
                    self.ytdl_subtitles_autogenerated = bool(cfg['ytdl']["subtitles"]['autogenerated'])

            # Extra-args
            self.ytdl_extra_args = dict()
            if cfg['ytdl'].get('extra_args') != None:
                for key, value in cfg['ytdl']['extra_args'].items():
                    if value.lower() in ('true', 'false'):
                        self.ytdl_extra_args[key] = bool(value)
                    else:
                        try:
                            self.ytdl_extra_args[key] = int(value)
                        except ValueError:
                            self.ytdl_extra_args[key] = value
        except Exception as e:
            sys.exit(f"Error with ytdl config.yml values: {e}")

        # YTDL Setup
        try:
            self.series = cfg["series"]
        except Exception as e:
            sys.exit("Error with series config.yml values: {e}")

    def get_episodes_by_series_id(self, series_id):
        """Returns all episodes for the given series"""
        logger.debug('Begin call Sonarr for all episodes for series_id: {}'.format(series_id))
        args = {'seriesId': series_id}
        res = self.request_get("{}/{}/episode".format(
            self.base_url, 
            self.sonarr_api_version
            ), args
        )
        return res.json()

    def get_episode_files_by_series_id(self, series_id):
        """Returns all episode files for the given series"""
        res = self.request_get("{}/{}/episodefile?seriesId={}".format(
            self.base_url, 
            self.sonarr_api_version,
            series_id
        ))
        return res.json()

    def get_series(self):
        """Return all series in your collection"""
        logger.debug('Begin call Sonarr for all available series')
        res = self.request_get("{}/{}/series".format(
            self.base_url, 
            self.sonarr_api_version
        ))
        return res.json()

    def get_series_by_series_id(self, series_id):
        """Return the series with the matching ID or 404 if no matching series is found"""
        logger.debug('Begin call Sonarr for specific series series_id: {}'.format(series_id))
        res = self.request_get("{}/{}/series/{}".format(
            self.base_url,
            self.sonarr_api_version,
            series_id
        ))
        return res.json()

    def request_get(self, url, params=None):
        """Wrapper on the requests.get"""
        logger.debug('Begin GET with url: {}'.format(url))
        args = {
            "apikey": self.api_key
        }
        if params is not None:
            logger.debug('Begin GET with params: {}'.format(params))
            args.update(params)
        url = "{}?{}".format(
            url,
            urllib.parse.urlencode(args)
        )
        res = requests.get(url)
        return res

    def request_put(self, url, params=None, jsondata=None):
        logger.debug('Begin PUT with url: {}'.format(url))
        """Wrapper on the requests.put"""
        headers = {
            'Content-Type': 'application/json',
        }
        args = (
            ('apikey', self.api_key),
        )
        if params is not None:
            args.update(params)
            logger.debug('Begin PUT with params: {}'.format(params))
        res = requests.post(
            url,
            headers=headers,
            params=args,
            json=jsondata
        )
        return res

    def rescanseries(self, series_id):
        """Refresh series information from trakt and rescan disk"""
        logger.debug('Begin call Sonarr to rescan for series_id: {}'.format(series_id))
        data = {
            "name": "RescanSeries",
            "seriesId": str(series_id)
        }
        res = self.request_put(
            "{}/{}/command".format(self.base_url, self.sonarr_api_version),
            None, 
            data
        )
        return res.json()

    def filterseries(self):
        """Return all series in Sonarr that are to be downloaded by youtube-dl"""
        series = self.get_series()
        matched = []
        for ser in series[:]:
            for wnt in self.series:
                if wnt['title'] == ser['title']:
                    # Set default values
                    ser['playlistreverse'] = True

                    ser['subtitles'] = self.ytdl_subtitles
                    if ser['subtitles']:
                        ser['subtitles_languages'] = self.ytdl_subtitles_languages
                        ser['subtitles_autogenerated'] = self.ytdl_subtitles_autogenerated

                    # Update values
                    if 'regex' in wnt:
                        regex = wnt['regex']
                        if 'sonarr' in regex:
                            ser['sonarr_regex_match'] = regex['sonarr']['match']
                            ser['sonarr_regex_replace'] = regex['sonarr']['replace']
                        if 'site' in regex:
                            ser['site_regex_match'] = regex['site']['match']
                            ser['site_regex_replace'] = regex['site']['replace']
                    if 'offset' in wnt:
                        ser['offset'] = wnt['offset']
                    if 'cookies_file' in wnt:
                        ser['cookies_file'] = wnt['cookies_file']
                    if 'format' in wnt:
                        ser['format'] = wnt['format']
                    if 'playlistreverse' in wnt:
                        if wnt['playlistreverse'] == 'False':
                            ser['playlistreverse'] = False
                    if 'subtitles' in wnt:
                        ser['subtitles'] = True
                        if 'languages' in wnt['subtitles']:
                            ser['subtitles_languages'] = wnt['subtitles']['languages']
                        if 'autogenerated' in wnt['subtitles']:
                            ser['subtitles_autogenerated'] = wnt['subtitles']['autogenerated']
                    ser['url'] = wnt['url']
                    matched.append(ser)
        for check in matched:
            if not check['monitored']:
                logger.warn('{0} is not currently monitored'.format(ser['title']))
        del series[:]
        return matched

    def getseriesepisodes(self, series):
        needed = []
        for ser in series[:]:
            episodes = self.get_episodes_by_series_id(ser['id'])
            for eps in episodes[:]:
                eps_date = now
                if "airDateUtc" in eps:
                    eps_date = datetime.strptime(eps['airDateUtc'], date_format)
                    if 'offset' in ser:
                        eps_date = offsethandler(eps_date, ser['offset'])
                if not eps['monitored']:
                    episodes.remove(eps)
                elif eps['hasFile']:
                    episodes.remove(eps)
                elif eps_date > now:
                    episodes.remove(eps)
                else:
                    if 'sonarr_regex_match' in ser:
                        match = ser['sonarr_regex_match']
                        replace = ser['sonarr_regex_replace']
                        eps['title'] = re.sub(match, replace, eps['title'])
                    needed.append(eps)
                    continue
            if len(episodes) == 0:
                logger.info('{0} no episodes needed'.format(ser['title']))
                series.remove(ser)
            else:
                logger.info('{0} missing {1} episodes'.format(
                    ser['title'],
                    len(episodes)
                ))
                for i, e in enumerate(episodes):
                    logger.info('  {0}: {1} - {2}'.format(
                        i + 1,
                        ser['title'],
                        e['title']
                    ))
        return needed

    def append_extra_args(self, ytdlopts):
        return {**ytdlopts, **self.ytdl_extra_args}

    def appendcookie(self, ytdlopts, cookies=None):
        """Checks if specified cookie file exists in config
        - ``ytdlopts``: Youtube-dl options to append cookie to
        - ``cookies``: filename of cookie file to append to Youtube-dl opts
        returns:
            ytdlopts
                original if problem with cookies file
                updated with cookies value if cookies file exists
        """
        if cookies is not None:
            cookie_path = os.path.abspath(CONFIGPATH + cookies)
            cookie_exists = os.path.exists(cookie_path)
            if cookie_exists is True:
                ytdlopts.update({
                    'cookiefile': cookie_path
                })
                # if self.debug is True:
                logger.debug('  Cookies file used: {}'.format(cookie_path))
            if cookie_exists is False:
                logger.warning('  cookie files specified but doesn''t exist.')
            return ytdlopts
        else:
            return ytdlopts

    def customformat(self, ytdlopts, customformat=None):
        """Checks if specified cookie file exists in config
        - ``ytdlopts``: Youtube-dl options to change the ytdl format for
        - ``customformat``: format to download
        returns:
            ytdlopts
                original: if no custom format
                updated: with new format value if customformat exists
        """
        if customformat is not None:
            ytdlopts.update({
                'format': customformat
            })
            return ytdlopts
        else:
            return ytdlopts

    def ytdl_eps_search_opts(self, regextitle, playlistreverse, cookies=None):
        ytdlopts = {
            'ignoreerrors': True,
            'playlistreverse': playlistreverse,
            'matchtitle': regextitle,
            'quiet': True,

        }
        if self.debug is True:
            ytdlopts.update({
                'quiet': False,
                'logger': YoutubeDLLogger(),
                'progress_hooks': [ytdl_hooks],
            })
        ytdlopts = self.appendcookie(ytdlopts, cookies)
        ytdlopts = self.append_extra_args(ytdlopts)
        if self.debug is True:
            logger.debug('Youtube-DL opts used for episode matching')
            logger.debug(ytdlopts)
        return ytdlopts

    def ytsearch(self, ydl_opts, playlist, name):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(
                    playlist,
                    download=False
                )
        except Exception as e:
            logger.error(e)
        else:
            video_url = None
            if 'entries' in result and len(result['entries']) > 0:
                try:
                    titles = [entry['title'].lower() for entry in result['entries']]
                    index = find_best_match_index(titles, name.lower())
                    video_url = result['entries'][index].get('webpage_url')
                except Exception as e:
                    logger.error(e)
            else:
                video_url = result.get('webpage_url')
            if playlist == video_url:
                return False, ''
            if video_url is None:
                logger.error('No video_url')
                return False, ''
            else:
                return True, video_url

    def download(self, series, episodes):
        if len(series) != 0:
            logger.info("Processing Wanted Downloads")
            for s, ser in enumerate(series):
                logger.info("  {}:".format(ser['title']))
                for e, eps in enumerate(episodes):
                    if ser['id'] == eps['seriesId']:
                        cookies = None
                        url = ser['url']
                        if 'cookies_file' in ser:
                            cookies = ser['cookies_file']
                        ydleps = self.ytdl_eps_search_opts(upperescape(eps['title']), ser['playlistreverse'], cookies)
                        found, dlurl = self.ytsearch(ydleps, url, name=ser['title']+' - '+eps['title'])
                        if found:
                            logger.info("    {}: Found - {}:".format(e + 1, eps['title']))
                            ytdl_format_options = {
                                'format': self.ytdl_format,
                                'quiet': True,
                                'merge-output-format': 'mp4',
                                'outtmpl': '/sonarr_root{0}/Season {1}/{2} - S{1}E{3} - {4} WEBDL.%(ext)s'.format(
                                    ser['path'],
                                    eps['seasonNumber'],
                                    ser['title'],
                                    eps['episodeNumber'],
                                    eps['title']
                                ),
                                'progress_hooks': [ytdl_hooks],
                                'noplaylist': True,
                            }
                            ytdl_format_options = self.appendcookie(ytdl_format_options, cookies)
                            if 'format' in ser:
                                ytdl_format_options = self.customformat(ytdl_format_options, ser['format'])
                            if 'subtitles' in ser:
                                if ser['subtitles']:
                                    postprocessors = []
                                    postprocessors.append({
                                        'key': 'FFmpegSubtitlesConvertor',
                                        'format': 'srt',
                                    })
                                    postprocessors.append({
                                        'key': 'FFmpegEmbedSubtitle',
                                    })
                                    ytdl_format_options.update({
                                        'writesubtitles': True,
                                        'writeautomaticsub': ser['subtitles_autogenerated'],
                                        'subtitleslangs': ser['subtitles_languages'],
                                        'postprocessors': postprocessors,
                                    })


                            if self.debug is True:
                                ytdl_format_options.update({
                                    'quiet': False,
                                    'logger': YoutubeDLLogger(),
                                    'progress_hooks': [ytdl_hooks_debug],
                                })
                                logger.debug('Youtube-DL opts used for downloading')
                                logger.debug(ytdl_format_options)
                            try:
                                yt_dlp.YoutubeDL(ytdl_format_options).download([dlurl])
                                logger.info("      Downloaded - {}".format(eps['title']))
                            except Exception as e:
                                logger.error("      Failed - {} - {}".format(eps['title'], e))
                        else:
                            logger.info("    {}: Missing - {}:".format(e + 1, eps['title']))
            self.rescanseries(ser['id'])
        else:
            logger.info("Nothing to process")

    def set_scan_interval(self, interval):
        global SCANINTERVAL
        if interval != SCANINTERVAL:
            SCANINTERVAL = interval
            logger.info('Scan interval set to every {} minutes by config.yml'.format(interval))
        else:
            logger.info('Default scan interval of every {} minutes in use'.format(interval))
        return


def main():
    config_file = os.path.abspath(CONFIGFILE)
    config_file_exists = os.path.exists(config_file)
    if config_file_exists:
        client = SonarrYTDL()
        series = client.filterseries()
        episodes = client.getseriesepisodes(series)
        client.download(series, episodes)
        logger.info('Waiting...')
    else:
        logger.warning('Configuration file not found. Skipping episode watching part.')

        def monitor_config_file():
            try:
                import inotify.adapters
                notifier = inotify.adapters.Inotify()
                notifier.add_watch(config_file)

                for event in notifier.event_gen(yield_nones=False):
                    (_, type_names, path, filename) = event
                    if 'IN_MODIFY' in type_names:
                        logger.info('Configuration file modified. Rerunning main.')
                        main()
            except Exception as e:
                logger.error(f"Error monitoring config file: {e}")

        monitor_thread = threading.Thread(target=monitor_config_file)
        monitor_thread.start()

if __name__ == "__main__":
    logger.info('Initial run')
    def run_flask():
        app.run(host='0.0.0.0', port=5050)

    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    main()
    schedule.every(int(SCANINTERVAL)).minutes.do(main)
    while True:
        schedule.run_pending()
        time.sleep(1)
