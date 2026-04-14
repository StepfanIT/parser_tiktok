from app.cli import TikTokCli
from app.config import load_app_config
from app.logging_config import configure_logging
from app.repositories.account_repository import AccountRepository
from app.repositories.csv_repository import CSVRepository
from app.services.comment_service import TikTokCommentService


def main() -> None:
    config = load_app_config()
    logger = configure_logging(config)
    service = TikTokCommentService(
        config=config,
        logger=logger,
        account_repository=AccountRepository(config),
        csv_repository=CSVRepository(config),
    )
    cli = TikTokCli(config=config, logger=logger, service=service)
    cli.run()


if __name__ == "__main__":
    main()