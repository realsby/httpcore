import cgi
import typing

import chardet
import idna
import rfc3986

from .config import SSLConfig, TimeoutConfig
from .constants import codes
from .decoders import (
    ACCEPT_ENCODING,
    SUPPORTED_DECODERS,
    Decoder,
    IdentityDecoder,
    MultiDecoder,
)
from .exceptions import InvalidURL, ResponseClosed, ResponseNotRead, StreamConsumed
from .utils import (
    get_reason_phrase,
    is_known_encoding,
    normalize_header_key,
    normalize_header_value,
)

URLTypes = typing.Union["URL", str]

HeaderTypes = typing.Union[
    "Headers",
    typing.Dict[typing.AnyStr, typing.AnyStr],
    typing.List[typing.Tuple[typing.AnyStr, typing.AnyStr]],
]

ByteOrByteStream = typing.Union[bytes, typing.AsyncIterator[bytes]]


class URL:
    def __init__(self, url: URLTypes, allow_relative: bool = False) -> None:
        if isinstance(url, rfc3986.uri.URIReference):
            self.components = url
        elif isinstance(url, str):
            self.components = rfc3986.api.uri_reference(url)
        else:
            self.components = url.components

        # Handle IDNA domain names.
        if self.components.authority:
            idna_authority = self.components.authority.encode("idna").decode("ascii")
            if idna_authority != self.components.authority:
                self.components = self.components.copy_with(authority=idna_authority)

        # Normalize scheme and domain name.
        self.components = self.components.normalize()

        # Enforce absolute URLs by default.
        if not allow_relative:
            if not self.scheme:
                raise InvalidURL("No scheme included in URL.")
            if self.scheme not in ("http", "https"):
                raise InvalidURL('URL scheme must be "http" or "https".')
            if not self.host:
                raise InvalidURL("No host included in URL.")

    @property
    def scheme(self) -> str:
        return self.components.scheme or ""

    @property
    def authority(self) -> str:
        return self.components.authority or ""

    @property
    def host(self) -> str:
        return self.components.host or ""

    @property
    def port(self) -> int:
        port = self.components.port
        if port is None:
            return {"https": 443, "http": 80}[self.scheme]
        return int(port)

    @property
    def path(self) -> str:
        return self.components.path or "/"

    @property
    def query(self) -> str:
        return self.components.query or ""

    @property
    def full_path(self) -> str:
        path = self.path
        if self.query:
            path += "?" + self.query
        return path

    @property
    def fragment(self) -> str:
        return self.components.fragment or ""

    @property
    def is_ssl(self) -> bool:
        return self.components.scheme == "https"

    @property
    def is_absolute_url(self) -> bool:
        """
        Return `True` for absolute URLs such as 'http://example.com/path',
        and `False` for relative URLs such as '/path'.
        """
        # We don't use rfc3986's `is_absolute` because it treats
        # URLs with a fragment portion as not absolute.
        # What we actually care about is if the URL provides
        # a scheme and hostname to which connections should be made.
        return self.components.scheme and self.components.host

    @property
    def is_relative_url(self) -> bool:
        return not self.is_absolute_url

    @property
    def origin(self) -> "Origin":
        return Origin(self)

    def copy_with(self, **kwargs: typing.Any) -> "URL":
        return URL(self.components.copy_with(**kwargs))

    def resolve_with(self, base_url: URLTypes) -> "URL":
        """
        Return an absolute URL, using base_url as the base.
        """
        # We drop any fragment portion, because RFC 3986 strictly
        # treats URLs with a fragment portion as not being absolute URLs.
        base_url = URL(base_url).copy_with(fragment=None)
        return URL(self.components.resolve_with(base_url.components))

    def __hash__(self) -> int:
        return hash(str(self))

    def __eq__(self, other: typing.Any) -> bool:
        return isinstance(other, URL) and str(self) == str(other)

    def __str__(self) -> str:
        return self.components.unsplit()

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        url_str = str(self)
        return f"{class_name}({url_str!r})"


class Origin:
    def __init__(self, url: URLTypes) -> None:
        if not isinstance(url, URL):
            url = URL(url)
        self.is_ssl = url.is_ssl
        self.host = url.host
        self.port = url.port

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, self.__class__)
            and self.is_ssl == other.is_ssl
            and self.host == other.host
            and self.port == other.port
        )

    def __hash__(self) -> int:
        return hash((self.is_ssl, self.host, self.port))


class Headers(typing.MutableMapping[str, str]):
    """
    A case-insensitive multidict.
    """

    def __init__(self, headers: HeaderTypes = None, encoding: str = None) -> None:
        if headers is None:
            self._list = []  # type: typing.List[typing.Tuple[bytes, bytes]]
        elif isinstance(headers, Headers):
            self._list = list(headers.raw)
        elif isinstance(headers, dict):
            self._list = [
                (normalize_header_key(k, encoding), normalize_header_value(v, encoding))
                for k, v in headers.items()
            ]
        else:
            self._list = [
                (normalize_header_key(k, encoding), normalize_header_value(v, encoding))
                for k, v in headers
            ]
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        """
        Header encoding is mandated as ascii, but utf-8 or iso-8859-1 may be
        seen in the wild.
        """
        if self._encoding is None:
            for encoding in ["ascii", "utf-8"]:
                for key, value in self.raw:
                    try:
                        key.decode(encoding)
                        value.decode(encoding)
                    except UnicodeDecodeError:
                        break
                else:
                    # The else block runs if 'break' did not occur, meaning
                    # all values fitted the encoding.
                    self._encoding = encoding
                    break
            else:
                # The ISO-8859-1 encoding covers all 256 code points in a byte,
                # so will never raise decode errors.
                self._encoding = "iso-8859-1"
        return self._encoding

    @encoding.setter
    def encoding(self, value: str) -> None:
        self._encoding = value

    @property
    def raw(self) -> typing.List[typing.Tuple[bytes, bytes]]:
        """
        Returns a list of the raw header items, as byte pairs.
        May be mutated in-place.
        """
        return self._list

    def keys(self) -> typing.List[str]:  # type: ignore
        return [key.decode(self.encoding) for key, value in self._list]

    def values(self) -> typing.List[str]:  # type: ignore
        return [value.decode(self.encoding) for key, value in self._list]

    def items(self) -> typing.List[typing.Tuple[str, str]]:  # type: ignore
        return [
            (key.decode(self.encoding), value.decode(self.encoding))
            for key, value in self._list
        ]

    def get(self, key: str, default: typing.Any = None) -> typing.Any:
        try:
            return self[key]
        except KeyError:
            return default

    def getlist(self, key: str, split_commas: bool = False) -> typing.List[str]:
        """
        Return multiple header values.
        """
        get_header_key = key.lower().encode(self.encoding)

        values = [
            item_value.decode(self.encoding)
            for item_key, item_value in self._list
            if item_key == get_header_key
        ]

        if not split_commas:
            return values

        split_values = []
        for value in values:
            split_values.extend([item.strip() for item in value.split(",")])
        return split_values

    def __getitem__(self, key: str) -> str:
        """
        Return a single header value.

        If there are multiple headers with the same key, then we concatenate
        them with commas. See: https://tools.ietf.org/html/rfc7230#section-3.2.2
        """
        normalized_key = key.lower().encode(self.encoding)

        items = []
        for header_key, header_value in self._list:
            if header_key == normalized_key:
                items.append(header_value.decode(self.encoding))

        if items:
            return ", ".join(items)

        raise KeyError(key)

    def __setitem__(self, key: str, value: str) -> None:
        """
        Set the header `key` to `value`, removing any duplicate entries.
        Retains insertion order.
        """
        set_key = key.lower().encode(self.encoding)
        set_value = value.encode(self.encoding)

        found_indexes = []
        for idx, (item_key, item_value) in enumerate(self._list):
            if item_key == set_key:
                found_indexes.append(idx)

        for idx in reversed(found_indexes[1:]):
            del self._list[idx]

        if found_indexes:
            idx = found_indexes[0]
            self._list[idx] = (set_key, set_value)
        else:
            self._list.append((set_key, set_value))

    def __delitem__(self, key: str) -> None:
        """
        Remove the header `key`.
        """
        del_key = key.lower().encode(self.encoding)

        pop_indexes = []
        for idx, (item_key, item_value) in enumerate(self._list):
            if item_key == del_key:
                pop_indexes.append(idx)

        for idx in reversed(pop_indexes):
            del self._list[idx]

    def __contains__(self, key: typing.Any) -> bool:
        get_header_key = key.lower().encode(self.encoding)
        for header_key, header_value in self._list:
            if header_key == get_header_key:
                return True
        return False

    def __iter__(self) -> typing.Iterator[typing.Any]:
        return iter(self.keys())

    def __len__(self) -> int:
        return len(self._list)

    def __eq__(self, other: typing.Any) -> bool:
        if not isinstance(other, Headers):
            return False
        return sorted(self._list) == sorted(other._list)

    def __repr__(self) -> str:
        class_name = self.__class__.__name__

        encoding_str = ""
        if self.encoding != "ascii":
            encoding_str = f", encoding={self.encoding!r}"

        as_dict = dict(self.items())
        if len(as_dict) == len(self):
            return f"{class_name}({as_dict!r}{encoding_str})"
        as_list = self.items()
        return f"{class_name}({as_list!r}{encoding_str})"


class Request:
    def __init__(
        self,
        method: str,
        url: typing.Union[str, URL],
        *,
        headers: HeaderTypes = None,
        content: ByteOrByteStream = b"",
    ):
        self.method = method.upper()
        self.url = URL(url) if isinstance(url, str) else url
        if isinstance(content, bytes):
            self.is_streaming = False
            self.content = content
        else:
            self.is_streaming = True
            self.content_aiter = content
        self.headers = Headers(headers)

    async def read(self) -> bytes:
        """
        Read and return the response content.
        """
        if not hasattr(self, "content"):
            content = b""
            async for part in self.stream():
                content += part
            self.content = content
        return self.content

    async def stream(self) -> typing.AsyncIterator[bytes]:
        if self.is_streaming:
            async for part in self.content_aiter:
                yield part
        elif self.content:
            yield self.content

    def prepare(self) -> None:
        """
        Adds in any default headers. When using the `Client`, this will
        end up being called into by the `prepare_request()` stage.

        You can omit this behavior by calling `Client.send()` with an
        explicitly built `Request` instance.
        """
        auto_headers = []  # type: typing.List[typing.Tuple[bytes, bytes]]

        has_host = "host" in self.headers
        has_content_length = (
            "content-length" in self.headers or "transfer-encoding" in self.headers
        )
        has_accept_encoding = "accept-encoding" in self.headers

        if not has_host and self.url.authority:
            auto_headers.append((b"host", self.url.authority.encode("ascii")))
        if not has_content_length:
            if self.is_streaming:
                auto_headers.append((b"transfer-encoding", b"chunked"))
            elif self.content:
                content_length = str(len(self.content)).encode()
                auto_headers.append((b"content-length", content_length))
        if not has_accept_encoding:
            auto_headers.append((b"accept-encoding", ACCEPT_ENCODING.encode()))

        for item in reversed(auto_headers):
            self.headers.raw.insert(0, item)

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        url = str(self.url)
        return f"<{class_name}({self.method}, {url})>"


class Response:
    def __init__(
        self,
        status_code: int,
        *,
        reason_phrase: str = None,
        protocol: str = None,
        headers: HeaderTypes = None,
        content: ByteOrByteStream = b"",
        on_close: typing.Callable = None,
        request: Request = None,
        history: typing.List["Response"] = None,
    ):
        try:
            # Use a StatusCode IntEnum if possible, for a nicer representation.
            self.status_code = codes(status_code)  # type: int
        except ValueError:
            self.status_code = status_code
        self.reason_phrase = reason_phrase or get_reason_phrase(status_code)
        self.protocol = protocol
        self.headers = Headers(headers)

        if isinstance(content, bytes):
            self.is_closed = True
            self.is_stream_consumed = True
            self._raw_content = content
        else:
            self.is_closed = False
            self.is_stream_consumed = False
            self._raw_stream = content

        self.on_close = on_close
        self.request = request
        self.history = [] if history is None else list(history)
        self.next = None  # typing.Optional[typing.Callable]

    @property
    def url(self) -> typing.Optional[URL]:
        """
        Returns the URL for which the request was made.

        Requires that `request` was provided when instantiating the response.
        """
        return None if self.request is None else self.request.url

    @property
    def content(self) -> bytes:
        if not hasattr(self, "_content"):
            if hasattr(self, "_raw_content"):
                content = self.decoder.decode(self._raw_content)
                content += self.decoder.flush()
                self._content = content
            else:
                raise ResponseNotRead()
        return self._content

    @property
    def text(self) -> str:
        if not hasattr(self, "_text"):
            content = self.content
            if not content:
                self._text = ""
            else:
                encoding = self.encoding
                self._text = content.decode(encoding, errors="replace")
        return self._text

    @property
    def encoding(self) -> str:
        if not hasattr(self, "_encoding"):
            encoding = self.charset_encoding
            if encoding is None or not is_known_encoding(encoding):
                encoding = self.apparent_encoding
                if encoding is None or not is_known_encoding(encoding):
                    encoding = "utf-8"
            self._encoding = encoding
        return self._encoding

    @encoding.setter
    def encoding(self, value: str) -> None:
        self._encoding = value

    @property
    def charset_encoding(self) -> typing.Optional[str]:
        """
        Return the encoding, as specified by the Content-Type header.
        """
        content_type = self.headers.get("Content-Type")
        if content_type is None:
            return None

        parsed = cgi.parse_header(content_type)
        media_type, params = parsed[0], parsed[-1]
        if "charset" in params:
            return params["charset"].strip("'\"")

        # RFC 2616 specifies that 'iso-8859-1' should be used as the default
        # for 'text/*' media types, if no charset is provided.
        # See: https://www.w3.org/Protocols/rfc2616/rfc2616-sec3.html#sec3.7.1
        if media_type.startswith("text/"):
            return "iso-8859-1"

        return None

    @property
    def apparent_encoding(self) -> typing.Optional[str]:
        """
        Return the encoding, as it appears to autodetection.
        """
        return chardet.detect(self.content)["encoding"]

    @property
    def decoder(self) -> Decoder:
        """
        Returns a decoder instance which can be used to decode the raw byte
        content, depending on the Content-Encoding used in the response.
        """
        if not hasattr(self, "_decoder"):
            decoders = []  # type: typing.List[Decoder]
            values = self.headers.getlist("content-encoding", split_commas=True)
            for value in values:
                value = value.strip().lower()
                decoder_cls = SUPPORTED_DECODERS[value]
                decoders.append(decoder_cls())

            if len(decoders) == 1:
                self._decoder = decoders[0]
            elif len(decoders) > 1:
                self._decoder = MultiDecoder(decoders)
            else:
                self._decoder = IdentityDecoder()

        return self._decoder

    async def read(self) -> bytes:
        """
        Read and return the response content.
        """
        if not hasattr(self, "_content"):
            content = b""
            async for part in self.stream():
                content += part
            self._content = content
        return self._content

    async def stream(self) -> typing.AsyncIterator[bytes]:
        """
        A byte-iterator over the decoded response content.
        This allows us to handle gzip, deflate, and brotli encoded responses.
        """
        if hasattr(self, "_content"):
            yield self._content
        else:
            async for chunk in self.raw():
                yield self.decoder.decode(chunk)
            yield self.decoder.flush()

    async def raw(self) -> typing.AsyncIterator[bytes]:
        """
        A byte-iterator over the raw response content.
        """
        if hasattr(self, "_raw_content"):
            yield self._raw_content
        else:
            if self.is_stream_consumed:
                raise StreamConsumed()
            if self.is_closed:
                raise ResponseClosed()

            self.is_stream_consumed = True
            async for part in self._raw_stream:
                yield part
            await self.close()

    async def close(self) -> None:
        """
        Close the response and release the connection.
        Automatically called if the response body is read to completion.
        """
        if not self.is_closed:
            self.is_closed = True
            if self.on_close is not None:
                await self.on_close()

    @property
    def is_redirect(self) -> bool:
        return (
            self.status_code
            in (
                codes.moved_permanently,
                codes.found,
                codes.see_other,
                codes.temporary_redirect,
                codes.permanent_redirect,
            )
            and "location" in self.headers
        )

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"<{class_name}(status_code={self.status_code})>"
