pipeline {
  agent {
    kubernetes {
      label 'dind-agent'
    }
  }

  environment {
    GCP_PROJECT_PROD = 'ny-prod'
    GCP_AR_PROD = "asia-south1-docker.pkg.dev/${GCP_PROJECT_PROD}"
    IMAGE_REPO = 'argus'
    IMAGE_NAME = 'vishwakarma'
  }

  stages {
    stage('Initialize') {
      steps {
        script {
          env.LAST_COMMIT_HASH = sh(script: "git rev-parse --short HEAD", returnStdout: true).trim()
        }
      }
    }

    stage('Deploy to GCP Production') {
      steps {
        withCredentials([file(credentialsId: 'gcp-sa-key-prod', variable: 'GCP_KEY_FILE_PROD')]) {
          script {
            echo "Building vishwakarma (Argus) @ ${env.LAST_COMMIT_HASH} for GCP Production"

            sh "docker build --no-cache -t ${env.IMAGE_NAME}:prod-gcp ."

            // Login
            sh 'cat $GCP_KEY_FILE_PROD | docker login -u _json_key --password-stdin https://asia-south1-docker.pkg.dev'

            // Tag and push — commit-hash tag only, no floating `latest`
            sh "docker tag ${env.IMAGE_NAME}:prod-gcp ${env.GCP_AR_PROD}/${env.IMAGE_REPO}/${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH}"
            sh "docker push ${env.GCP_AR_PROD}/${env.IMAGE_REPO}/${env.IMAGE_NAME}:${env.LAST_COMMIT_HASH}"
          }
        }
      }
    }
  }
}
