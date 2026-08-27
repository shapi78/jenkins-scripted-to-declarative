pipeline {
    agent none
    stages {
        stage('Build on Linux') {
            agent { label "linux" }
            steps {
                sh './build.sh'
            }
        }
        stage('Build on Windows') {
            agent { label "windows" }
            steps {
                bat 'build.bat'
            }
        }
    }
}
