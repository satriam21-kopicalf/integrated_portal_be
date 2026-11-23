"""Social Media Analytics engine (migration 135): the sentiment word list and the relevance filter."""
from app.social import relevant, sentiment


def test_sentiment_word_list():
    assert sentiment("Kopi Calf enak banget, mantap!") == "positive"
    assert sentiment("kecewa, kopinya basi dan mahal") == "negative"
    assert sentiment("Kopi Calf buka cabang baru di Madiun") == "neutral"
    assert sentiment("gak enak sih menurutku") == "negative"


def test_only_what_names_kopi_calf():
    assert relevant("Review KOPI CALF Kemang")
    assert relevant(None, "#kopicalf pagi ini")
    assert not relevant("Calf muscle workout", "leg day")


def test_labels_every_keyword_a_post_names():
    from app.social import labels
    t = "Podcast bareng Danang Yuda Prawira, Founder Kopi Calf, soal Calf On The Road #TemanCalf"
    assert labels(t) == ["Kopi Calf", "Calf On The Road", "Danang Yuda Prawira", "Founder Kopi Calf", "Teman Calf"]
    assert labels("Ridzky Novasandro, Co-Founder Kopi Calf") == ["Kopi Calf", "Ridzky Novasandro", "Co-Founder Kopi Calf"]
    assert labels("Calf To Go di stasiun") == ["Calf To Go"]
    assert labels("calf muscle") == []


def test_words_and_cities():
    from app.social import _cities, _words
    w = {x["word"]: x["n"] for x in _words(["Opening Kopi Calf Madiun, antri panjang", "Kopi Calf Madiun ramai antri", "yang dan di"])}
    assert w.get("madiun") == 2 and w.get("antri") == 2 and "yang" not in w and "kopi" not in w
    c = _cities(["Buka di Madiun", "Calf Tegal dan Madiun", "Jogja"])
    assert c[0] == {"city": "Madiun", "n": 2, "share": 50.0} and any(x["city"] == "Yogyakarta" for x in c)


def test_a_source_stopped_early_keeps_its_rows(monkeypatch):
    from app import social
    calls = []

    def fake_news(kw, since=None):
        calls.append(kw)
        if kw == social.KEYWORDS[1]:
            raise social.Partial([{"post_id": "b"}], "429")
        return [{"post_id": kw}]
    monkeypatch.setattr(social, "news", fake_news)
    try:
        social.collect("news")
        raise AssertionError("expected Partial")
    except social.Partial as e:
        assert [r["post_id"] for r in e.rows] == [social.KEYWORDS[0], "b"]  # the first keyword's rows are not lost
