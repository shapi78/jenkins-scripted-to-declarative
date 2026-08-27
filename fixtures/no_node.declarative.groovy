import groovy.json.JsonSlurper

def notifySlack(msg) {
    echo "slack: ${msg}"
}

pipeline {
    agent any
    stages {
        stage('Build') {
            steps {
                sh 'make'
                notifySlack('build done')
            }
        }
        stage('Test') {
            steps {
                sh 'make test'
            }
        }
    }
}

def cleanupHelper() {
    echo 'cleanup'
}
