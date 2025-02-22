from typing import Any, Callable, Dict, List, cast

import anyio
from nonebot import get_available_plugin_names, logger, require
from pyncm import (
    DumpSessionAsString,
    GetCurrentSession,
    LoadSessionFromString,
    SetCurrentSession,
)
from pyncm.apis import WeapiCryptoRequest
from pyncm.apis.cloudsearch import GetSearchResult
from pyncm.apis.login import (
    GetCurrentLoginStatus,
    LoginViaAnonymousAccount,
    LoginViaCellphone,
    LoginViaEmail,
)
from pyncm.apis.track import GetTrackAudio, GetTrackDetail, GetTrackLyrics

from .config import config
from .const import DATA_PATH
from .types import (
    LyricData,
    Privilege,
    Song,
    SongSearchResult,
    TrackAudio,
    VoiceBaseInfo,
    VoiceSearchResult,
)
from .utils import awaitable

SESSION_FILE = DATA_PATH / "session.cache"


def get_offset_by_page_num(page: int, limit: int = config.ncm_list_limit) -> int:
    return limit * (page - 1)


async def ncm_request(api: Callable, *args, **kwargs) -> Dict[str, Any]:
    ret = await awaitable(api)(*args, **kwargs)
    if ret.get("code", 200) != 200:
        raise RuntimeError(f"请求 {api.__name__} 失败\n{ret}")
    logger.debug(f"{api.__name__} - {ret}")
    return ret


async def search_song(keyword: str, page: int = 1, **kwargs) -> SongSearchResult:
    offset = get_offset_by_page_num(page)
    res = await ncm_request(
        GetSearchResult,
        keyword=keyword,
        limit=config.ncm_list_limit,
        offset=offset,
        **kwargs,
    )
    return SongSearchResult(**res["result"])


async def search_voice(keyword: str, page: int = 1) -> VoiceSearchResult:
    @WeapiCryptoRequest  # type: ignore
    def SearchVoice():  # noqa: N802
        return (
            "/api/search/voice/get",
            {
                "keyword": keyword,
                "scene": "normal",
                "limit": config.ncm_list_limit,
                "offset": offset or 0,
            },
        )

    offset = get_offset_by_page_num(page)
    res = await ncm_request(SearchVoice)
    return VoiceSearchResult(**res["data"])


async def get_track_audio(
    song_ids: List[int],
    bit_rate: int = 320000,
    **kwargs,
) -> List[TrackAudio]:
    res = await ncm_request(GetTrackAudio, song_ids, bitrate=bit_rate, **kwargs)
    return [TrackAudio(**x) for x in cast(List[dict], res["data"])]


async def get_track_info(ids: List[int], **kwargs) -> List[Song]:
    res = await ncm_request(GetTrackDetail, ids, **kwargs)
    privileges = {y.id: y for y in [Privilege(**x) for x in res["privileges"]]}
    return [
        Song(
            **x,
            privilege=(
                privileges[song_id]
                if (song_id := x["id"]) in privileges
                else Privilege(id=song_id, pl=128000)  # , plLevel="standard")
            ),
        )
        for x in res["songs"]
    ]


async def get_track_lrc(song_id: int) -> LyricData:
    res = await ncm_request(GetTrackLyrics, song_id)
    return LyricData(**res)


async def get_voice_info(program_id: int) -> VoiceBaseInfo:
    @WeapiCryptoRequest  # type: ignore
    def GetProgramDetail():  # noqa: N802
        return ("/api/dj/program/detail", {"id": program_id})

    res = await ncm_request(GetProgramDetail)
    return VoiceBaseInfo(**res["program"])


async def login(retry=True):
    if SESSION_FILE.exists():
        logger.info(f"使用缓存登录态 ({SESSION_FILE})")
        SetCurrentSession(
            LoadSessionFromString(
                (await anyio.Path(SESSION_FILE).read_text(encoding="u8")),
            ),
        )

    elif (config.ncm_phone or config.ncm_email) and (
        config.ncm_password or config.ncm_password_hash
    ):
        retry = False

        if config.ncm_phone:
            logger.info("使用手机号登录")
            await awaitable(LoginViaCellphone)(
                ctcode=config.ncm_ctcode,
                phone=config.ncm_phone,
                password=config.ncm_password or "",
                passwordHash=config.ncm_password_hash or "",
            )

        else:
            logger.info("使用邮箱登录")
            await awaitable(LoginViaEmail)(
                email=config.ncm_email or "",
                password=config.ncm_password or "",
                passwordHash=config.ncm_password_hash or "",
            )

        await anyio.Path(SESSION_FILE).write_text(
            DumpSessionAsString(GetCurrentSession()),
            encoding="u8",
        )

    else:
        retry = False
        logger.warning("账号或密码未填写，使用游客账号登录")
        await awaitable(LoginViaAnonymousAccount)()

    try:
        ret = cast(dict, await awaitable(GetCurrentLoginStatus)())
        assert ret["code"] == 200
        assert ret["account"]
    except Exception as e:
        if await (pth := anyio.Path(SESSION_FILE)).exists():
            await pth.unlink()

        if retry:
            logger.warning("恢复缓存会话失败，尝试使用正常流程登录")
            await login(retry=False)
            return

        raise RuntimeError("登录态异常，请重新登录") from e

    session = GetCurrentSession()
    logger.info(f"登录成功，欢迎您，{session.nickname} [{session.uid}]")


if "nonebot-plugin-ncm" in get_available_plugin_names():
    logger.info("nonebot-plugin-ncm 已安装，本插件将依赖其全局 Session")
    require("nonebot-plugin-ncm")

else:
    from nonebot import get_driver

    driver = get_driver()

    @driver.on_startup
    async def _():
        await login()
