pipeline {
    agent any

    options {
        disableConcurrentBuilds()
        timestamps()
    }

    environment {
        IMAGE_REPOSITORY = 'arabic-pdf2epub'
        IMAGE_REF = "arabic-pdf2epub:jenkins-${BUILD_NUMBER}"
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Validate Compose') {
            steps {
                sh 'docker compose config -q'
            }
        }

        stage('Test Python 3.14') {
            options {
                timeout(time: 10, unit: 'MINUTES')
            }
            steps {
                sh '''
                    docker run --rm \
                        --volume "$PWD:/src:ro" \
                        --workdir /src \
                        python:3.14-slim \
                        sh -c "cp -R /src /tmp/project && cd /tmp/project && pip install '.[dev]' && pytest -q && ruff check ."
                '''
            }
        }

        stage('Build Image') {
            steps {
                sh 'docker build --pull --tag "$IMAGE_REF" .'
            }
        }

        stage('Verify Runtime Tools') {
            steps {
                sh '''
                    set -eu
                    docker run --rm "$IMAGE_REF" epubcheck --version
                    languages="$(docker run --rm "$IMAGE_REF" tesseract --list-langs 2>&1)"
                    printf '%s\n' "$languages"
                    printf '%s\n' "$languages" | grep -qx ara
                    printf '%s\n' "$languages" | grep -qx eng
                '''
            }
        }

        stage('Smoke Test') {
            steps {
                sh '''
                    set -eu
                    container_name="pdf2epub-jenkins-${BUILD_NUMBER}"
                    cleanup() {
                        docker stop "$container_name" >/dev/null 2>&1 || true
                    }
                    trap cleanup EXIT
                    docker run --rm -d --name "$container_name" "$IMAGE_REF"

                    attempt=0
                    until docker exec "$container_name" python -c \
                        "import json, urllib.request; response=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)); assert response['status'] == 'ok' and response['epubcheck_available']"; do
                        attempt=$((attempt + 1))
                        if [ "$attempt" -ge 15 ]; then
                            docker logs "$container_name"
                            exit 1
                        fi
                        sleep 1
                    done
                '''
            }
        }
    }
}
