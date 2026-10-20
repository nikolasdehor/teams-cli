"""Extração de anexos: imagens AMS, arquivos SharePoint, gravações, cartões (xteams e Graph)."""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teams_cli import attachments as att
from teams_cli import context as ctxmod

AMS = "https://us-api.asm.skype.com/v1/objects/0-eus-d13-abc123/views/imgo"
SP_DOC = "https://empresa-my.sharepoint.com/:w:/r/personal/ana/Documents/Plano%20Q4.docx?d=w1&csf=1"
SP_VIDEO = "https://empresa-my.sharepoint.com/:v:/g/personal/ana/IQAB?e=x"
SP_FOLDER = "https://empresa-my.sharepoint.com/:f:/g/personal/ana/Eabc"


def xmsg(content, mtype="RichText/Html", mid="100", **extra):
    return {"id": mid, "messagetype": mtype, "content": content, "imdisplayname": "Ana", **extra}


def swift(card, summary="resumo"):
    payload = {"summary": summary, "attachments": [{"content": card}]}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_ams_inline_image():
    html = f'<p>olha</p><img src="{AMS}" itemtype="http://schema.skype.com/AMSImage" width="635.6" height="250" alt="imagem" itemid="0-eus-d13-abc123">'
    (a,) = att.extract_from_xteams(xmsg(html))
    assert (a["kind"], a["source"], a["id"], a["width"], a["height"]) == ("image", "ams", "0-eus-d13-abc123", 635, 250)
    assert a["url"] == AMS and a["downloadable"] and a["message_id"] == "100"
    assert set(a) == {"id", "message_id", "kind", "name", "mime", "size", "width", "height", "source", "url",
                      "text", "downloadable", "note"}


def test_emoji_is_not_an_attachment():
    html = ('<img itemtype="http://schema.skype.com/Emoji" src="https://statics.teams.cdn.office.net/'
            'evergreen-assets/personal-expressions/v2/assets/emoticons/cool/default/20_f.png">')
    assert att.extract_from_xteams(xmsg(html)) == []


def test_external_image_marked_not_auto_downloadable_note():
    (a,) = att.extract_from_xteams(xmsg('<img src="https://media.giphy.com/media/x/giphy.gif" alt="Image">'))
    assert a["source"] == "external" and "--external" in a["note"]


def test_http_and_unknown_image_hosts_ignored():
    assert att.extract_from_xteams(xmsg('<img src="http://x.com/a.png"><img src="data:image/png;base64,AAAA">')) == []


def test_ams_lookalike_host_is_not_trusted():
    (a,) = att.extract_from_xteams(xmsg('<img src="https://evil.com/asm.skype.com/v1/objects/x/views/imgo">'))
    assert a["source"] == "external"
    (b,) = att.extract_from_xteams(xmsg('<img src="https://asm.skype.com.evil.com/v1/objects/x/views/imgo">'))
    assert b["source"] == "external"


def test_sharepoint_files_videos_folders():
    html = (
        f'<a href="{SP_DOC}" itemtype="http://schema.skype.com/HyperLink/Files">Plano Q4.docx</a>'
        f'<a href="{SP_VIDEO}">demo</a>'
        f'<a href="{SP_FOLDER}">pasta</a>'
        '<a href="https://example.com/x.pdf">site comum</a>'
    )
    doc, vid, folder = att.extract_from_xteams(xmsg(html))
    assert (doc["kind"], doc["name"], doc["downloadable"], doc["source"]) == ("file", "Plano Q4.docx", True, "sharepoint")
    assert doc["mime"].endswith("wordprocessingml.document")
    assert vid["kind"] == "video" and vid["downloadable"]
    assert folder["kind"] == "folder" and not folder["downloadable"]


def test_ids_are_stable_across_calls():
    html = f'<a href="{SP_DOC}">Plano Q4.docx</a>'
    first, second = att.extract_from_xteams(xmsg(html)), att.extract_from_xteams(xmsg(html))
    assert first[0]["id"] == second[0]["id"]


def test_call_recording_ready_and_ongoing():
    ready = (
        '<URIObject type="Video.2/CallRecording.1" uri=""><Title>Daily</Title>'
        f'<a href="{SP_VIDEO}">Play</a><OriginalName v="Daily-Gravacao.mp4" />'
        '<Identifiers><Id type="AMSDocumentID" value="0-wus-d5-doc" /></Identifiers></URIObject>'
    )
    ongoing = '<URIObject type="Video.2/CallRecording.1" uri=""><Title>Daily</Title><a href="">Play</a></URIObject>'
    (r,) = att.extract_from_xteams(xmsg(ready, "RichText/Media_CallRecording"))
    assert (r["kind"], r["id"], r["name"], r["downloadable"]) == ("recording", "rec-0-wus-d5-doc", "Daily-Gravacao.mp4", True)
    (o,) = att.extract_from_xteams(xmsg(ongoing, "RichText/Media_CallRecording"))
    assert not o["downloadable"] and "andamento" in o["note"]


def test_transcript_message():
    (t,) = att.extract_from_xteams(xmsg('{\\"scopeId\\":\\"x\\"}', "RichText/Media_CallTranscript"))
    assert t["kind"] == "transcript" and not t["downloadable"]


def test_adaptive_card_text_from_swift():
    card = {
        "type": "AdaptiveCard",
        "body": [
            {"type": "TextBlock", "text": "Deploy concluído"},
            {"type": "FactSet", "facts": [{"title": "Versão", "value": "1.2.3"}]},
            {"type": "ColumnSet", "columns": [{"items": [{"type": "TextBlock", "text": "coluna A"}]}]},
            {"type": "Container", "items": [{"type": "TextBlock", "text": "dentro"}]},
        ],
        "actions": [{"type": "Action.OpenUrl", "title": "Abrir", "url": "https://x.io"}],
    }
    html = f'<URIObject type="SWIFT.1"><Title>Deploy</Title><Swift b64="{swift(card)}"/></URIObject>'
    (c,) = att.extract_from_xteams(xmsg(html, "RichText/Media_Card"))
    assert c["kind"] == "card" and not c["downloadable"]
    for piece in ("Deploy concluído", "Versão: 1.2.3", "coluna A", "dentro", "[ação: Abrir -> https://x.io]"):
        assert piece in c["text"]


def test_card_with_garbage_b64_does_not_crash():
    html = '<URIObject type="SWIFT.1"><Title>T</Title><Swift b64="@@@"/></URIObject>'
    (c,) = att.extract_from_xteams(xmsg(html, "RichText/Media_Card"))
    assert c["kind"] == "card" and c["text"] == "T"


def test_legacy_uriobject_picture_audio_file():
    pic = '<URIObject type="Picture.1" uri="https://us-api.asm.skype.com/v1/objects/0-eus-1"><OriginalName v="foto.png"/></URIObject>'
    aud = '<URIObject type="Audio.1" uri="https://us-api.asm.skype.com/v1/objects/0-eus-2"><Title>voz</Title></URIObject>'
    fil = '<URIObject type="File.1" uri="https://us-api.asm.skype.com/v1/objects/0-eus-3"><OriginalName v="a.pdf"/></URIObject>'
    p, a, f = (att.extract_from_xteams(xmsg(h, "RichText/Media_Generic"))[0] for h in (pic, aud, fil))
    assert (p["kind"], p["url"].endswith("/views/imgo")) == ("image", True)
    assert (a["kind"], a["url"].endswith("/views/audio")) == ("audio", True)
    assert (f["kind"], f["url"].endswith("/views/original"), f["mime"]) == ("file", True, "application/pdf")


def test_placeholder_without_details_is_flagged():
    (u,) = att.extract_from_xteams(xmsg('<attachment id="abc-1"></attachment>'))
    assert u["kind"] == "unknown" and "graph" in u["note"]


def test_properties_files_and_cards():
    props = {
        "files": json.dumps([{"fileName": "orc.xlsx", "objectUrl": SP_DOC}]),
        "cards": json.dumps([{"content": {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "oi"}]}}]),
    }
    f, c = att.extract_from_xteams(xmsg("", properties=props))
    assert f["name"] == "orc.xlsx" and f["downloadable"] and c["text"] == "oi"


def test_malformed_html_never_raises():
    assert att.extract_from_xteams(xmsg("<img src=<<<<URIObject <a href=")) == []
    assert att.extract_from_xteams({"id": "1"}) == []
    assert att.extract_from_xteams(xmsg(None)) == []


def test_graph_message_attachments_and_hosted():
    msg = {
        "id": "9",
        "body": {
            "contentType": "html",
            "content": '<img src="https://graph.microsoft.com/v1.0/chats/c/messages/9/hostedContents/aGk=/$value" alt="x" width="10" height="20">',
        },
        "attachments": [
            {"id": "g1", "contentType": "reference", "contentUrl": SP_DOC, "name": "Plano.docx"},
            {"id": "g2", "contentType": "application/vnd.microsoft.card.adaptive",
             "content": json.dumps({"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "cartão Graph"}]})},
        ],
    }
    ref, card, img = att.extract_from_graph(msg)
    assert ref["source"] == "graph-reference" and ref["downloadable"] and ref["kind"] == "file"
    assert card["text"] == "cartão Graph"
    assert img["source"] == "graph-hosted" and img["id"] == "h-aGk=" and img["width"] == 10


def test_annotate_adds_key_to_every_message_and_keeps_non_lists():
    msgs = [xmsg("sem anexo"), xmsg(f'<img src="{AMS}">', mid="2")]
    att.annotate(msgs)
    assert msgs[0]["attachments"] == [] and msgs[1]["attachments"][0]["kind"] == "image"
    assert att.annotate({"a": 1}) == {"a": 1}


def test_render_and_describe():
    msgs = [xmsg(f'<img src="{AMS}" width="10" height="20" alt="foto">')]
    att.annotate(msgs)
    msgs[0]["attachments"][0]["size"] = 2048
    lines = att.render_human(msgs)
    assert lines[0] == "anexos:" and "[image] foto (2.0 KB 10x20)" in lines[1]
    assert att.render_human([xmsg("oi", **{"attachments": []})]) == []
    assert att.fmt_size(None) == "?" and att.fmt_size(5) == "5 B" and att.fmt_size(5 * 1024 * 1024) == "5.0 MB"


def test_context_shows_attachments_and_summarizes_media_messages():
    card = {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": "Pipeline verde"}]}
    msgs = [
        xmsg(f'<p>veja</p><img src="{AMS}" alt="img">', mid="1", sequenceId=1,
             originalarrivaltime="2026-10-19T10:00:00Z"),
        xmsg(f'<URIObject type="SWIFT.1"><Title>Bot</Title><Swift b64="{swift(card)}"/></URIObject>',
             "RichText/Media_Card", mid="2", sequenceId=2, originalarrivaltime="2026-10-19T10:01:00Z"),
    ]
    tz = ctxmod.get_tz("America/Sao_Paulo")
    one = ctxmod.shape_message(msgs[0], None, tz)
    two = ctxmod.shape_message(msgs[1], None, tz)
    assert one["text"] == "veja" and one["attachments"][0]["kind"] == "image"
    assert two["text"].startswith("[cartão]") and "Pipeline verde" in two["text"]


def test_graph_relative_hosted_image_is_resolved_against_chat_message():
    msg = {"id": "9", "chatId": "19:abc@thread.v2",
           "body": {"contentType": "html", "content": '<img src="../hostedContents/aGk=/$value" alt="x">'}}
    (img,) = att.extract_from_graph(msg)
    assert img["source"] == "graph-hosted" and img["downloadable"]
    assert img["url"] == "https://graph.microsoft.com/v1.0/chats/19:abc@thread.v2/messages/9/hostedContents/aGk=/$value"


def test_graph_relative_hosted_image_in_channel_message():
    msg = {"id": "9", "chatId": None, "channelIdentity": {"teamId": "t1", "channelId": "19:c@thread.tacv2"},
           "body": {"contentType": "html", "content": '<img src="../hostedContents/aGk=/$value">'}}
    (img,) = att.extract_from_graph(msg)
    assert img["url"] == "https://graph.microsoft.com/v1.0/teams/t1/channels/19:c@thread.tacv2/messages/9/hostedContents/aGk=/$value"


def test_malformed_urls_do_not_abort_extraction():
    html = '<a href="https://[">x</a><img src="https://[">' + f'<img src="{AMS}">'
    (img,) = att.extract_from_xteams(xmsg(html))
    assert img["source"] == "ams"


def test_each_unmatched_placeholder_is_kept():
    html = f'<a href="{SP_DOC}">Plano Q4.docx</a><attachment id="p1"></attachment><attachment id="p2"></attachment>'
    kinds = sorted(a["kind"] for a in att.extract_from_xteams(xmsg(html)))
    assert kinds == ["file", "unknown"]


def test_infinite_image_dimensions_are_ignored():
    (img,) = att.extract_from_xteams(xmsg(f'<img src="{AMS}" width="1e309" height="-1e309">'))
    assert img["width"] is None and img["height"] is None


def test_card_text_tolerates_null_collections():
    card = {"type": "AdaptiveCard", "body": [{"type": "RichTextBlock", "inlines": None},
                                             {"type": "FactSet", "facts": None}, {"type": "TextBlock", "text": "ok"}]}
    assert att.card_text(card) == "ok"
