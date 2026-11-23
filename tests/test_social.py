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
