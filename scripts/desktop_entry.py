"""Entry point for the standalone Windows build."""

if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()

    from mopa_luiz.desktop import main

    raise SystemExit(main())
