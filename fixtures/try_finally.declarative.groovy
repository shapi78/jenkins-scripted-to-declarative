pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'make'
            }
        }
        stage('Test') {
            steps {
                sh 'make test'
            }
        }
    }
    post {
        failure {
            script {
                        echo 'build failed'
                        throw e
            }
        }
        always {
            deleteDir()
            archiveArtifacts artifacts: 'build/*.log', allowEmptyArchive: true
        }
    }
}
