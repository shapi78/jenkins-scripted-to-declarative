node {
    try {
        stage('Build') {
            sh 'make'
        }
        stage('Test') {
            sh 'make test'
        }
    } catch (e) {
        echo 'build failed'
        throw e
    } finally {
        deleteDir()
        archiveArtifacts artifacts: 'build/*.log', allowEmptyArchive: true
    }
}
