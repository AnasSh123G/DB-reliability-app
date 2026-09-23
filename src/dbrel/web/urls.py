from __future__ import annotations

from django.urls import path, re_path

from dbrel.web import views

urlpatterns = [
    path("api/suggest", views.suggest, name="suggest"),
    path("api/station/<int:station_id>/board", views.station_board, name="station-board"),
    path("api/train/<int:train_id>/route", views.train_route, name="train-route"),
    path("api/line/<int:line_id>/directions", views.line_directions, name="line-directions"),
    path("api/line/<int:line_id>/stations", views.line_stations, name="line-stations"),
    path("api/line/<int:line_id>/around", views.line_trips_around, name="line-trips-around"),
    path("api/line/<int:line_id>/detail", views.line_detail, name="line-detail"),
    # Everything else belongs to the bahn-buddy-predict frontend, which dbrel
    # serve runs as its own subprocess -- must stay last, since it matches any
    # path none of the /api/* patterns above already claimed.
    re_path(r"^(?P<path>.*)$", views.frontend_proxy, name="frontend"),
]
