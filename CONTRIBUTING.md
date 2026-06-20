# Contributing to Meeting Intelligence Agent

Thank you for your interest in contributing!

## Development Setup

1. Clone the repository
2. Create virtual environment: `python -m venv venv`
3. Activate: `source venv/bin/activate` (or `venv\Scripts\activate` on Windows)
4. Install dependencies: `pip install -r requirements.txt`
5. Install FFmpeg for audio processing
6. Start services: `docker-compose up -d`

## Running Tests

```bash
pytest tests/
pytest --cov=src tests/  # With coverage
```

## Code Style

- Use Black for formatting: `black src/ tests/`
- Follow PEP 8 guidelines
- Add docstrings to all functions
- Write tests for new features

## Pull Request Process

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Write/update tests
5. Ensure tests pass
6. Format code with Black
7. Commit (`git commit -m 'Add amazing feature'`)
8. Push to branch
9. Open Pull Request

## Areas for Contribution

- Improving transcription accuracy
- Adding new calendar integrations
- Enhancing AI summarization
- UI/UX improvements
- Documentation
- Bug fixes

## Questions?

Open an issue or email: support@useagenticai.in
