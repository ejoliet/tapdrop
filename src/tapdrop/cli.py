"""Command line interface.

The flag surface here is the CLI contract in ``RDD.md``; it is fixed even where
the behaviour behind a flag arrives in a later milestone. Keeping the surface
complete means ``tapdrop serve --help`` is honest about the target shape and the
env-var mapping (``TAPDROP_<FLAG>``) never drifts from the flags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tapdrop import __version__
from tapdrop.registry import Registry

app = typer.Typer(
    name="tapdrop",
    help="Serve catalog or image files as a temporary, standards-compliant TAP endpoint.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """tapdrop command line."""


@app.command()
def serve(
    sources: Annotated[
        list[str] | None,
        typer.Argument(help="Folders, globs, s3:// or https:// URIs holding catalog files."),
    ] = None,
    images: Annotated[
        list[str] | None,
        typer.Option("--images", help="Image directories or URIs (v1.1)."),
    ] = None,
    host: Annotated[str, typer.Option(help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8000,
    ttl: Annotated[
        str | None,
        typer.Option(help="Service lifetime, e.g. 2h, 24h, 7d. Defaults to 24h with --share."),
    ] = None,
    share: Annotated[
        bool, typer.Option("--share", help="Start a cloudflared quick tunnel and print URL + QR.")
    ] = False,
    token: Annotated[
        bool, typer.Option("--token", help="Generate a secret; serve under /t/<token>/.")
    ] = False,
    allow_upload: Annotated[
        bool, typer.Option("--allow-upload", help="Enable drag-drop ingest and TAP UPLOAD.")
    ] = False,
    config: Annotated[
        Path | None, typer.Option("--config", help="tapdrop.yaml with discovery overrides.")
    ] = None,
    result_store: Annotated[
        str | None, typer.Option("--result-store", help="Directory or s3:// prefix for results.")
    ] = None,
    max_rows: Annotated[int, typer.Option(help="Default MAXREC.")] = 100_000,
    hard_max_rows: Annotated[int, typer.Option(help="Upper bound on MAXREC.")] = 10_000_000,
    query_timeout: Annotated[int, typer.Option(help="Sync query timeout in seconds.")] = 300,
    memory_limit: Annotated[str, typer.Option(help="DuckDB memory_limit.")] = "75%",
    cache: Annotated[
        bool, typer.Option("--cache", help="Copy remote columns to local DuckDB on first use.")
    ] = False,
    log_dir: Annotated[Path | None, typer.Option("--log-dir", help="Enable the query log.")] = None,
) -> None:
    """Serve SOURCES as a TAP endpoint."""
    import secrets

    import uvicorn

    from tapdrop.api.tap import create_app
    from tapdrop.config import Settings
    from tapdrop.discovery import discover
    from tapdrop.engine import create_connection
    from tapdrop.querylog import configure_logging, redact_token_in_access_log
    from tapdrop.share import TunnelError, start_tunnel, stop_when_expired, terminal_qr

    configure_logging()
    settings = Settings(
        sources=sources or [],
        images=images or [],
        host=host,
        port=port,
        ttl=ttl,
        share=share,
        # The token is minted here, not taken from the flag: a secret typed on a
        # command line lands in the shell history.
        token=secrets.token_urlsafe(32) if token else None,
        allow_upload=allow_upload,
        config_file=config,
        max_rows=max_rows,
        hard_max_rows=hard_max_rows,
        query_timeout=query_timeout,
        memory_limit=memory_limit,
        cache=cache,
        log_dir=log_dir,
    )
    if result_store:
        settings.result_store = result_store
    settings.down_at = settings.resolve_down_at()
    if settings.token:
        redact_token_in_access_log(settings.token)

    registry = discover(settings.sources, settings.config_file)
    # Images are attached further down, once the public base URL is known, so
    # an images-only service (RDD.md's `tapdrop serve --images ./roman_l2/`)
    # legitimately has no catalog tables here.
    if not registry.tables and not settings.images:
        typer.secho("No tables discovered. Nothing to serve.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    for skipped in registry.skipped:
        typer.secho(f"skipped {skipped.uri}: {skipped.reason}", fg=typer.colors.YELLOW, err=True)

    connection = create_connection(settings, registry)
    for name in sorted(registry.tables):
        typer.echo(f"  {name}")

    tunnel = None
    if share:
        try:
            public_url, tunnel = start_tunnel(settings.port)
        except TunnelError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from exc
        settings.public_url = public_url
        typer.echo(terminal_qr(f"{public_url}{settings.root_path}/tap"))

    # 0.0.0.0 is a bind address, not an address a client can reach; the Docker
    # image sets it, and an ObsCore access_url built from it points nowhere.
    reachable_host = "127.0.0.1" if settings.host == "0.0.0.0" else settings.host
    base = settings.public_url or f"http://{reachable_host}:{settings.port}"

    if settings.images:
        # After the tunnel, because artifact access_urls embed the public base
        # URL, and that is only known once --share has a URL to embed.
        from tapdrop.caom_lite import attach_images

        image_skipped = attach_images(
            connection, registry, settings.images, f"{base}{settings.root_path}"
        )
        for image in image_skipped:
            typer.secho(f"skipped {image.uri}: {image.reason}", fg=typer.colors.YELLOW, err=True)
        count = connection.execute('SELECT count(*) FROM "ivoa"."obscore"').fetchone()
        typer.echo(f"  ivoa.obscore ({count[0] if count else 0} images)")

    typer.echo(f"TAP endpoint: {base}{settings.root_path}/tap")
    if settings.down_at:
        typer.echo(f"Expires at:   {settings.down_at.isoformat()}")

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, registry, connection), host=settings.host, port=settings.port
        )
    )
    if settings.down_at:
        stop_when_expired(server, settings.down_at)
    try:
        server.run()
    finally:
        if tunnel is not None:
            tunnel.terminate()


@app.command()
def inspect(
    sources: Annotated[list[str], typer.Argument(help="Sources to describe without serving.")],
    config: Annotated[
        Path | None, typer.Option("--config", help="tapdrop.yaml with discovery overrides.")
    ] = None,
) -> None:
    """Print discovered tables, columns, RA/Dec guesses, confidence and unresolved fields."""
    from tapdrop.discovery import discover

    registry = discover(sources, config)
    typer.echo(format_inspect(registry))


@app.command()
def scan(
    source: Annotated[str, typer.Argument(help="Image directory or URI to scan (headers only).")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the discovery YAML.")] = Path(
        "tapdrop.discovered.yaml"
    ),
) -> None:
    """v1.1: write tapdrop.discovered.yaml for a set of images."""
    from tapdrop.discovery.scan import scan_images, write_discovered_yaml

    catalog = scan_images(source)
    write_discovered_yaml(catalog, out)
    typer.echo(f"Discovered {len(catalog.observations)} image(s). Wrote {out}")
    for skipped in catalog.skipped:
        typer.secho(f"skipped {skipped.uri}: {skipped.reason}", fg=typer.colors.YELLOW, err=True)


@app.command()
def export(
    out: Annotated[Path, typer.Argument(help="Output directory for CAOM2 XML.")],
    images: Annotated[
        list[str] | None,
        typer.Option(
            "--images", help="Image directory or URI to export. Defaults to TAPDROP_IMAGES."
        ),
    ] = None,
    caom2_xml: Annotated[
        bool, typer.Option("--caom2-xml", help="Write one CAOM2 XML document per observation.")
    ] = False,
) -> None:
    """v1.1: export discovered observations in an archive-ready format."""
    from tapdrop.caom2_export import CaomExportError, export_caom2
    from tapdrop.config import Settings
    from tapdrop.discovery.scan import scan_sources

    if not caom2_xml:
        raise typer.BadParameter("--caom2-xml is the only export format.", param_hint="--caom2-xml")

    sources = images if images else Settings().images
    if not sources:
        raise typer.BadParameter("Nothing to export. Pass --images or set TAPDROP_IMAGES.")

    catalog = scan_sources(sources)
    try:
        written = export_caom2(catalog, out)
    except CaomExportError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote {len(written)} CAOM2 document(s) to {out}")
    for skipped in catalog.skipped:
        typer.secho(f"skipped {skipped.uri}: {skipped.reason}", fg=typer.colors.YELLOW, err=True)


def format_inspect(registry: Registry) -> str:
    """Stable, human-readable rendering of a Registry for ``tapdrop inspect``.

    Deterministic ordering (tables and skipped files both sorted) so this can
    be used as a golden-output test.
    """
    lines: list[str] = []
    tables = sorted(registry.tables.values(), key=lambda t: t.qualified_name)

    if not tables:
        lines.append("No tables discovered.")
    for meta in tables:
        lines.append(f"{meta.qualified_name} ({len(meta.columns)} columns)")
        if meta.description:
            lines.append(f"  description: {meta.description}")
        if meta.hats_order is not None:
            lines.append(f"  hats_order: {meta.hats_order}")
        if meta.ra_column and meta.dec_column:
            lines.append(
                f"  ra/dec: {meta.ra_column}, {meta.dec_column} "
                f"(rule={meta.ra_dec_rule}, confidence={meta.ra_dec_confidence})"
            )
        else:
            lines.append("  ra/dec: not detected")
        if meta.unresolved:
            lines.append(f"  unresolved: {', '.join(meta.unresolved)}")
        lines.append("  columns:")
        for column in meta.columns:
            bits = [f"unit={column.unit}" if column.unit else None]
            bits.append(f"ucd={column.ucd}" if column.ucd else None)
            bits.append(f"description={column.description!r}" if column.description else None)
            extra = " [" + ", ".join(b for b in bits if b) + "]" if any(bits) else ""
            lines.append(f"    - {column.name} ({column.datatype}){extra}")

    if registry.skipped:
        lines.append("skipped:")
        for entry in sorted(registry.skipped, key=lambda s: s.uri):
            lines.append(f"  - {entry.uri}: {entry.reason}")

    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    app()
