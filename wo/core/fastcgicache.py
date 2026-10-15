"""WordOps per-site FastCGI cache zones"""
import hashlib
import os
import re
import shutil

from wo.core.logging import Log
from wo.core.nginx import check_config
from wo.core.services import WOService
from wo.core.shellexec import CommandExecutionError, WOShellExec
from wo.core.template import WOTemplate
from wo.core.variables import WOVar


class WOFastCGICache():
    """Per-site FastCGI cache zones.

    Disabled by default. Enable it in /etc/wo/wo.conf:

        [nginx]
        per-site-fastcgi-cache = true

    Every site using fastcgi_cache (wpfc) then gets its own cache zone and
    directory, and nginx-helper purges by removing files from that directory
    only. "Purge all" on one site (WordPress, theme or Elementor updates, the
    nginx-helper button...) no longer empties the cache of the other sites.
    """

    zones_conf = '/etc/nginx/conf.d/fastcgi-zones.conf'
    default_root = '/var/cache/nginx-sites'

    def __init__():
        pass

    def option(self, key, default):
        """Read an option from the [nginx] section of wo.conf"""
        try:
            if self.app.config.has_section('nginx'):
                value = self.app.config.get('nginx', key)
                if value is not None and str(value).strip() != '':
                    return str(value).strip()
        except Exception as e:
            Log.debug(self, "{0}".format(e))
        return default

    def enabled(self):
        """Return True if per-site FastCGI cache zones are enabled"""
        return WOFastCGICache.option(
            self, 'per-site-fastcgi-cache', 'false').lower() in (
                'true', 'yes', 'on', '1')

    def cache_root(self):
        """Directory that holds one cache directory per site"""
        return WOFastCGICache.option(
            self, 'fastcgi-cache-root', WOFastCGICache.default_root
        ).rstrip('/')

    def zone_name(domain):
        """Return a valid and unique nginx keys_zone name for a domain"""
        slug = re.sub(r'[^a-z0-9]+', '_', domain.lower()).strip('_')[:40]
        digest = hashlib.sha1(domain.encode('utf-8')).hexdigest()[:6]
        return 'WO_{0}_{1}'.format(slug, digest)

    def site_path(self, domain):
        """Cache directory of a site"""
        return '{0}/{1}'.format(WOFastCGICache.cache_root(self), domain)

    def render(self, zones, hosts):
        """Write the zones configuration and create the cache directories"""
        for zone in zones:
            if not os.path.isdir(zone['path']):
                os.makedirs(zone['path'], exist_ok=True)
            shutil.chown(zone['path'], 'www-data', 'root')
        data = dict(release=WOVar.wo_version, zones=zones, hosts=hosts)
        WOTemplate.deploy(self, WOFastCGICache.zones_conf,
                          'fastcgi-zones.mustache', data, overwrite=True)

    def ensure_default(self):
        """Make sure $wpfc_zone is always defined.

        wpfc-phpXX.conf uses $wpfc_zone, so the map must exist before nginx
        loads the common configuration, even when the feature is disabled.
        """
        if not os.path.isfile(WOFastCGICache.zones_conf):
            WOFastCGICache.render(self, [], [])

    def regenerate(self):
        """Rebuild the zones configuration from the WordOps site database.

        Returns True if the new configuration is valid. If nginx rejects it,
        the previous configuration is restored and False is returned.
        """
        zones = []
        hosts = []
        if WOFastCGICache.enabled(self):
            # imported here to keep wo.core independent from wo.cli
            from wo.cli.plugins.sitedb import getAllsites
            keys_zone = WOFastCGICache.option(
                self, 'fastcgi-cache-keys-zone', '10m')
            inactive = WOFastCGICache.option(
                self, 'fastcgi-cache-inactive', '7d')
            max_size = WOFastCGICache.option(
                self, 'fastcgi-cache-max-size', '2g')
            for site in getAllsites(self) or []:
                if site.cache_type != 'wpfc' or not site.is_enabled:
                    continue
                domain = site.sitename
                zone = WOFastCGICache.zone_name(domain)
                zones.append(dict(
                    zone=zone, path=WOFastCGICache.site_path(self, domain),
                    keys_zone=keys_zone, inactive=inactive,
                    max_size=max_size))
                if site.site_type == 'wpsubdomain':
                    # with "hostnames", ".example.com" matches subdomains too
                    names = ['.{0}'.format(domain)]
                else:
                    names = [domain, 'www.{0}'.format(domain)]
                hosts.extend(dict(host=name, zone=zone) for name in names)
        previous = None
        if os.path.isfile(WOFastCGICache.zones_conf):
            with open(WOFastCGICache.zones_conf, encoding='utf-8') as f:
                previous = f.read()
        WOFastCGICache.render(self, zones, hosts)
        if check_config(self):
            return True
        Log.warn(self, "Invalid FastCGI cache zones configuration, "
                 "restoring the previous one")
        if previous is not None:
            with open(WOFastCGICache.zones_conf, encoding='utf-8',
                      mode='w') as f:
                f.write(previous)
        return False

    def configure_wordpress(self, domain, webroot):
        """Point nginx-helper of a wpfc site to its own cache directory"""
        htdocs = '{0}/htdocs'.format(webroot)
        cache_path = '{0}/'.format(WOFastCGICache.site_path(self, domain))
        try:
            WOShellExec.cmd_exec(
                self, "/bin/bash -c \"{0} --allow-root --path={1} "
                "config set RT_WP_NGINX_HELPER_CACHE_PATH '{2}'\""
                .format(WOVar.wo_wpcli_path, htdocs, cache_path))
            WOShellExec.cmd_exec(
                self, "/bin/bash -c \"{0} --allow-root --path={1} "
                "--skip-plugins --skip-themes option patch update "
                "rt_wp_nginx_helper_options purge_method unlink_files\""
                .format(WOVar.wo_wpcli_path, htdocs))
        except CommandExecutionError as e:
            Log.debug(self, "{0}".format(e))
            Log.warn(self, "Unable to configure nginx-helper for the "
                     "per-site FastCGI cache of {0}".format(domain))

    def sync_site(self, domain, webroot, cache_type):
        """Apply the per-site cache configuration after a site change"""
        if not WOFastCGICache.enabled(self):
            return
        if not WOFastCGICache.regenerate(self):
            return
        if cache_type == 'wpfc' and os.path.isdir(
                '{0}/htdocs'.format(webroot)):
            WOFastCGICache.configure_wordpress(self, domain, webroot)
        WOService.reload_service(self, 'nginx')

    def remove_site(self, domain):
        """Remove the cache zone and directory of a deleted site"""
        if not WOFastCGICache.enabled(self):
            return
        if WOFastCGICache.regenerate(self):
            WOService.reload_service(self, 'nginx')
        path = WOFastCGICache.site_path(self, domain)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def clean(self):
        """Empty every per-site cache directory"""
        root = WOFastCGICache.cache_root(self)
        if not os.path.isdir(root):
            return
        for site in os.listdir(root):
            path = os.path.join(root, site)
            if not os.path.isdir(path):
                continue
            for entry in os.listdir(path):
                target = os.path.join(path, entry)
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    os.remove(target)
