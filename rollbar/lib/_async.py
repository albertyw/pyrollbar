import asyncio
import contextlib
import inspect
import logging
import sys
from unittest import mock

import httpx

import rollbar
from rollbar import DEFAULT_TIMEOUT
from rollbar.lib import transport, urljoin

log = logging.getLogger(__name__)

ALLOWED_HANDLERS = (
    'async',
    'httpx',
)


class RollbarAsyncError(Exception):
    ...


async def report_exc_info(
    exc_info=None, request=None, extra_data=None, payload_data=None, level=None, **kw
):
    """
    Asynchronously reports an exception to Rollbar, using exc_info (from calling sys.exc_info())

    exc_info: optional, should be the result of calling sys.exc_info(). If omitted, sys.exc_info() will be called here.
    request: optional, a Starlette, WebOb, Werkzeug-based or Sanic request object.
    extra_data: optional, will be included in the 'custom' section of the payload
    payload_data: optional, dict that will override values in the final payload
                  (e.g. 'level' or 'fingerprint')
    kw: provided for legacy purposes; unused.

    Example usage:

    rollbar.init(access_token='YOUR_PROJECT_ACCESS_TOKEN')

    async def func():
        try:
            do_something()
        except:
            await report_exc_info(sys.exc_info(), request, {'foo': 'bar'}, {'level': 'warning'})
    """
    with async_handler():
        try:
            return await call_later(
                _report_exc_info(
                    exc_info, request, extra_data, payload_data, level, **kw
                )
            )
        except Exception as e:
            log.exception('Exception while reporting exc_info to Rollbar. %r', e)


async def report_message(
    message, level='error', request=None, extra_data=None, payload_data=None, **kw
):
    """
    Asynchronously reports an arbitrary string message to Rollbar.

    message: the string body of the message
    level: level to report at. One of: 'critical', 'error', 'warning', 'info', 'debug'
    request: the request object for the context of the message
    extra_data: dictionary of params to include with the message. 'body' is reserved.
    payload_data: param names to pass in the 'data' level of the payload; overrides defaults.
    """

    with async_handler():
        try:
            return await call_later(
                _report_message(message, level, request, extra_data, payload_data)
            )
        except Exception as e:
            log.exception('Exception while reporting message to Rollbar. %r', e)


async def _report_exc_info(
    exc_info=None, request=None, extra_data=None, payload_data=None, level=None, **kw
):
    return rollbar.report_exc_info(
        exc_info, request, extra_data, payload_data, level, **kw
    )


async def _report_message(
    message, level='error', request=None, extra_data=None, payload_data=None, **kw
):
    return rollbar.report_message(message, level, request, extra_data, payload_data)


async def _post_api_httpx(path, payload_str, access_token=None):
    headers = {'Content-Type': 'application/json'}
    if access_token is not None:
        headers['X-Rollbar-Access-Token'] = access_token
    else:
        headers['X-Rollbar-Access-Token'] = rollbar.SETTINGS.get('access_token')

    proxy_cfg = {
        'proxy': rollbar.SETTINGS.get('http_proxy'),
        'proxy_user': rollbar.SETTINGS.get('http_proxy_user'),
        'proxy_password': rollbar.SETTINGS.get('http_proxy_password'),
    }
    proxies = transport._get_proxy_cfg(proxy_cfg)

    url = urljoin(rollbar.SETTINGS['endpoint'], path)
    async with httpx.AsyncClient(
        proxies=proxies, verify=rollbar.SETTINGS.get('verify_https', True)
    ) as client:
        resp = await client.post(
            url,
            data=payload_str,
            headers=headers,
            timeout=rollbar.SETTINGS.get('timeout', DEFAULT_TIMEOUT),
        )

    try:
        return rollbar._parse_response(path, access_token, payload_str, resp)
    except Exception as e:
        log.exception('Exception while posting item %r', e)


async def try_report(
    exc_info=None, request=None, extra_data=None, payload_data=None, level=None, **kw
):
    current_handler = rollbar.SETTINGS.get('handler')
    if not (current_handler in ALLOWED_HANDLERS or current_handler == 'default'):
        raise RollbarAsyncError('No async handler set.')

    return await report_exc_info(
        exc_info, request, extra_data, payload_data, level, **kw
    )


@contextlib.contextmanager
def async_handler():
    original_handler = rollbar.SETTINGS.get('handler')

    if original_handler not in ALLOWED_HANDLERS:
        log.warning(
            'Running coroutines requires async compatible handler. Switching to default async handler.'
        )
        rollbar.SETTINGS['handler'] = 'async'

    try:
        yield rollbar.SETTINGS['handler']
    finally:
        if original_handler is not None:
            rollbar.SETTINGS['handler'] = original_handler


def call_later(coro):
    if sys.version_info < (3, 7):
        return asyncio.ensure_future(coro)

    return asyncio.create_task(coro)


# test helpers
# TODO: move to rollbar.test.async_helper after migrating from unittest


def run(coro):
    if sys.version_info >= (3, 7):
        return asyncio.run(coro)

    assert inspect.iscoroutine(coro)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def async_receive(message):
    async def receive():
        return message

    assert message['type'] == 'http.request'
    return receive


async def coroutine():
    ...


class BareMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


class FailingTestASGIApp:
    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)

    async def app(self, scope, receive, send):
        raise RuntimeError('Invoked only for testing')


# for Python 3.7- compatibility
class AsyncMock(mock.MagicMock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
