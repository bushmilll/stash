"""Entry point for the packaged Mac app (Stash.app)."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from app.desktop import main
    main()
